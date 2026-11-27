"""A code sent to the person's phone: asked in the chat, typed in the portal and kept nowhere.

The chat is the application's own route, called in the test's loop; the portal is served from
memory and the «model» is a function. Nothing leaves this machine.
"""
import asyncio
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturador.account_flow import PHONE_QUESTION
from facturador.auth import read_json, write_private
from facturador.chat import SECRET_KEPT, SECRET_LATE, SECRET_THANKS, ChatDecision, chat_router, only_code
from facturador.db import Store
from facturador.portal_accounts import credential_file, site_password
from facturador.runner import Runner
from simulated_portal import ORIGIN, PAGE, Brain, Portal, ticket
from test_core import PROFILE, RECEIPT
from test_screen_plans import PortalCase, kinds

HOST = urlsplit(ORIGIN).hostname
QUESTION = PHONE_QUESTION % HOST
# Letters and digits: no timestamp nor identifier written by the program can contain it by chance.
CODE = 'K7Q2M9'
THANKS = 'Gracias. Sigo con el trámite con lo que me dijiste.'


def photo(color='red') -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (28, 30), color).save(output, 'PNG')
    return output.getvalue()


def chat_client(store, runner, plan):
    """The chat route of the application, alone and inside the caller's event loop."""
    app = FastAPI()
    app.include_router(chat_router(store, plan, Mock(), runner, lambda: PROFILE))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://panel.prueba')


class SecretChatCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.runner = Runner(self.store)
        self.addAsyncCleanup(self.runner.close)
        self.key = self.store.add('ticket.png', 'codigo-secreto')[0]['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)
        # A model that is ready and would read whatever the chat shows it.
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.plan.infer.return_value = ChatDecision(intent='conversation', reply='Dime en qué te ayudo.')
        self.client = chat_client(self.store, self.runner, self.plan)
        self.addAsyncCleanup(self.client.aclose)
        self.sent = 0

    async def say(self, text):
        self.sent += 1
        reply = await self.client.post('/api/chat', data={'text': text, 'client_id': f'mensaje_{self.sent:016d}'})
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()

    async def ask(self, question=QUESTION, *, secreto=True):
        """Start a question of the ticket and return its task once the chat can see it."""
        asking = asyncio.create_task(self.runner.ask_chat(self.key, question, secreto=secreto))
        async with asyncio.timeout(10):
            while self.runner.open_question() != self.key:
                await asyncio.sleep(.01)
        return asking

    async def unanswered(self):
        """A secret question that closed without an answer a moment ago."""
        with patch('facturador.runner.SECRET_ASK_TIMEOUT', 0.05):
            self.assertEqual(await self.runner.ask_chat(self.key, QUESTION, secreto=True), '')

    def in_progress(self):
        """The ticket is in the portal right now, as the chat sees it."""
        task = asyncio.ensure_future(asyncio.sleep(3600))
        self.runner.active_id, self.runner.job_tasks[self.key] = self.key, task

        async def stop():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.runner.active_id = None
            self.runner.job_tasks.clear()
        self.addAsyncCleanup(stop)

    async def everything(self):
        """All that was stored or shown: history, events, the panel's reading of the chat and the files."""
        shown = (await self.client.get('/api/chat')).json()
        files = b''.join(path.read_bytes() for path in self.store.root.glob('facturador.sqlite*')).decode('latin-1')
        return json.dumps([self.store.chat_messages(), self.store.events(self.key), shown, files], ensure_ascii=False)

    def assert_never_kept(self, text, *codes):
        for code in codes:
            self.assertNotIn(code.casefold(), text.casefold())


class SecretQuestionTests(SecretChatCase):
    async def test_secret_question_waits_longer_assumes_nothing_and_is_not_a_question_of_the_model(self):
        with patch('facturador.runner.ASK_TIMEOUT', 0.01), patch('facturador.runner.SECRET_ASK_TIMEOUT', 30):
            asking = await self.ask()
            await asyncio.sleep(0.2)                       # well past the wait of an ordinary question
            self.assertFalse(asking.done())
            self.assertTrue(self.runner.secret_open(self.key))
            self.assertEqual(self.runner.secret_recent(), self.key)
            self.runner.answer_question(self.key, CODE)
            self.assertEqual(await asking, CODE)
        message = self.store.chat_messages()[-1]['text']
        self.assertIn(QUESTION, message)
        self.assertIn('No guardo el código', message)
        self.assertNotIn('sigo con lo más probable', message)
        # Closed, it is still a secret for a while; and it never entered the record of repeated questions.
        self.assertFalse(self.runner.secret_open(self.key))
        self.assertEqual(self.runner.secret_recent(), self.key)
        self.assertEqual((self.runner.asked, self.runner.insisted), ({}, {}))
        self.assertEqual([event['detail'] for event in self.store.events(self.key) if event['kind'] == 'answer'],
                         ['Respuesta recibida en el chat.'])
        self.assert_never_kept(await self.everything(), CODE)

    async def test_without_an_answer_it_ends_empty_and_the_mark_expires(self):
        await self.unanswered()
        self.assertEqual([event['detail'] for event in self.store.events(self.key) if event['kind'] == 'answer'],
                         ['Sin respuesta en el chat.'])
        self.assertEqual(self.runner.secret_recent(), self.key)
        with patch('facturador.runner.SECRET_GRACE', -1):
            await self.unanswered()
        self.assertIsNone(self.runner.secret_recent())
        self.assertEqual(self.runner.secret_questions, {})

    async def test_an_ordinary_question_is_as_before(self):
        asking = await self.ask('¿Qué número de tienda uso?', secreto=False)
        self.assertFalse(self.runner.secret_open(self.key))
        self.assertIsNone(self.runner.secret_recent())
        self.assertIn('sigo con lo más probable', self.store.chat_messages()[-1]['text'])
        body = await self.say('La 2045')
        self.assertEqual(await asking, 'La 2045')
        self.assertEqual([message['text'] for message in body['messages']], ['La 2045', THANKS])
        with patch('facturador.runner.ASK_TIMEOUT', 0.05):
            self.assertEqual(await self.runner.ask_chat(self.key, '¿Y la caja?'), '')
        self.assertEqual(self.store.events(self.key)[-1]['detail'], 'Sin respuesta; el agente asume y sigue.')

    def test_what_counts_as_only_a_code(self):
        for text in ('482913', ' 482 913 ', '48-29-13', 'K7Q2M9', 'k7q2m9', 'es 482913', 'El código es: 482913.',
                     'mi clave es k7q2m9, gracias', 'ya me llegó: 4829'):
            with self.subTest(text):
                self.assertTrue(only_code(text))
        for text in ('', 'no me llegó', 'todavía nada', '12', '123456789012345', 'Pagué 1500 pesos con tarjeta',
                     'Usa el folio 88231 del ticket', '¿Cómo va mi ticket?', 'código'):
            with self.subTest(text):
                self.assertFalse(only_code(text))


class SecretAnswerTests(SecretChatCase):
    async def test_answer_reaches_who_waits_and_the_history_keeps_a_fixed_text(self):
        asking = await self.ask()
        body = await self.say(f'  {CODE}  ')
        self.assertEqual(await asking, CODE)
        self.assertEqual((body['intent'], body['job_ids']), ('answer', [self.key]))
        self.assertEqual([(message['role'], message['text']) for message in body['messages']],
                         [('user', SECRET_KEPT), ('assistant', SECRET_THANKS)])
        self.assertEqual(SECRET_KEPT, 'Código enviado.')
        # Sent again with the same identifier, the stored turn comes back: still without the code.
        again = await self.client.post('/api/chat', data={'text': CODE, 'client_id': 'mensaje_0000000000000001'})
        self.assertEqual(again.json()['messages'], body['messages'])
        self.assert_never_kept(await self.everything(), CODE)
        self.plan.infer.assert_not_called()
        self.assertEqual((self.runner.live_notes, self.runner.told), ({}, {}))

    async def test_late_code_is_not_passed_to_the_ticket_nor_to_the_model(self):
        await self.unanswered()
        self.in_progress()
        for text in (CODE, 'mi código es ' + CODE.lower(), '48 29 13'):
            body = await self.say(text)
            self.assertEqual([message['text'] for message in body['messages']], [SECRET_KEPT, SECRET_LATE])
            self.assertEqual((body['intent'], body['job_ids']), ('answer', [self.key]))
        self.assertIn('directamente en el portal', SECRET_LATE)
        self.assertEqual((self.runner.live_notes, self.runner.told), ({}, {}))
        self.assertNotIn('user_note', [event['kind'] for event in self.store.events(self.key)])
        self.plan.infer.assert_not_called()
        self.assert_never_kept(await self.everything(), CODE, '48 29 13')
        # Anything else still goes to the ticket in progress, as written.
        body = await self.say('Pagué 1500 pesos con tarjeta de débito')
        self.assertEqual(body['intent'], 'note')
        self.assertEqual(body['messages'][0]['text'], 'Pagué 1500 pesos con tarjeta de débito')
        self.assertEqual(len(self.runner.live_notes[self.key]), 1)

    async def test_late_code_without_a_ticket_in_progress_never_reaches_the_model(self):
        await self.unanswered()
        body = await self.say(CODE)
        self.assertEqual([message['text'] for message in body['messages']], [SECRET_KEPT, SECRET_LATE])
        self.plan.infer.assert_not_called()
        self.assert_never_kept(await self.everything(), CODE)
        # Once the mark expired, a number is an ordinary message again.
        self.runner.secret_questions[self.key] = time.monotonic() - 1
        body = await self.say('482913')
        self.assertEqual((body['intent'], body['messages'][0]['text']), ('conversation', '482913'))
        self.plan.infer.assert_called_once()

    async def test_code_typed_beside_a_photo_is_not_kept_either(self):
        await self.unanswered()
        self.sent += 1
        reply = await self.client.post('/api/chat', data={'text': CODE, 'client_id': f'mensaje_{self.sent:016d}'},
                                       files=[('files', ('ticket.png', photo(), 'image/png'))])
        self.assertEqual(reply.status_code, 200, reply.text)
        body = reply.json()
        # The photo follows its own way as a ticket; the text beside it is replaced like any late code.
        self.assertEqual((body['intent'], body['messages'][0]['text']), ('invoice', SECRET_KEPT))
        self.assertEqual(len(body['job_ids']), 1)
        self.assertEqual(len(body['messages'][0]['files']), 1)
        self.assertNotIn(CODE.casefold(), str(self.plan.infer.call_args_list).casefold())
        self.assert_never_kept(await self.everything(), CODE)
        # A caption that is not a code is kept as written.
        self.sent += 1
        reply = await self.client.post('/api/chat', data={'text': 'Factura este ticket', 'client_id': f'mensaje_{self.sent:016d}'},
                                       files=[('files', ('otro.png', photo('blue'), 'image/png'))])
        self.assertEqual(reply.json()['messages'][0]['text'], 'Factura este ticket')

    async def test_late_code_does_not_answer_another_question(self):
        await self.unanswered()
        asking = await self.ask('¿En qué sucursal compraste?', secreto=False)
        # While the mark lasts, a message that is only a code is taken for the late one, whatever
        # is open: a bare number is not handed to the other question either. Safer than leaking it.
        for text in (CODE, '2045'):
            body = await self.say(text)
            self.assertEqual([message['text'] for message in body['messages']], [SECRET_KEPT, SECRET_LATE])
            self.assertFalse(asking.done())
            self.assertEqual(self.runner.open_question(), self.key)
        body = await self.say('En la sucursal del centro')
        self.assertEqual(await asking, 'En la sucursal del centro')
        self.assertEqual([message['text'] for message in body['messages']], ['En la sucursal del centro', THANKS])
        self.assert_never_kept(await self.everything(), CODE)


SMS = ('<h1>Verifica tu teléfono</h1><p>Te enviamos un código por SMS al número terminado en 11.</p>'
       '<form method="post" action="/verificado"><label>Código<input name="codigo" autocomplete="one-time-code" '
       'maxlength="6" required></label><button>Verificar</button></form><a href="#">Reenviar código</a>')


class SmsPortal(Portal):
    """The billing portal of the engine tests behind a code sent to the phone."""

    def __init__(self):
        super().__init__()
        self.codes = []

    async def handle(self, route):
        request = route.request
        if urlsplit(request.url).path == '/verificado' and request.method == 'POST':
            self.codes.append(parse_qs(request.post_data or '').get('codigo', [''])[0])
            return await route.fulfill(content_type='text/html; charset=utf-8', body=self.lookup())
        if not self.codes:
            return await route.fulfill(content_type='text/html; charset=utf-8', body=PAGE % SMS)
        await super().handle(route)


class PhoneCodeTicketTests(PortalCase):
    """The engine with its own browser: the portal asks for the code and the person gives it in the chat."""

    async def test_code_typed_in_the_chat_reaches_the_portal_and_is_kept_nowhere(self):
        portal = await self.serve(SmsPortal())
        receipt = portal.add(ticket('TKT-55120-R', '2026-11-20', '312.40'))
        account = credential_file(self.store.root, portal.origin, PROFILE.email)
        brain = Brain()
        plan = Mock()
        plan.status.return_value = {'status': 'ready'}
        plan.infer.return_value = ChatDecision(intent='conversation', reply='Hola.')
        client = chat_client(self.store, self.runner, plan)
        self.addAsyncCleanup(client.aclose)
        key = self.add(receipt)
        # This ticket is opening its account on the portal: that is when a code may be asked for.
        site_password(account, True)
        write_private(account, {**read_json(account, {}), 'submission_attempted': True, 'registration_job': key,
                                'registration_started': time.time()})
        with patch('facturador.runner.AI.next_action', side_effect=brain):
            self.runner.schedule(key, PROFILE, auto_issue=True, confirmed_url=receipt.portal_url)
            task = self.runner.job_tasks[key]
            async with asyncio.timeout(60):
                while self.runner.open_question() != key:
                    self.assertFalse(task.done(), (self.store.get(key)['message'], self.store.events(key)[-6:]))
                    await asyncio.sleep(.03)
            self.assertTrue(self.runner.secret_open(key))
            self.assertEqual(self.store.chat_messages()[-1]['text'].split(': ', 1)[1].split(' No guardo')[0],
                             PHONE_QUESTION % urlsplit(portal.origin).hostname)
            reply = await client.post('/api/chat', data={'text': CODE, 'client_id': 'mensaje_0000000000000001'})
            self.assertEqual([message['text'] for message in reply.json()['messages']], [SECRET_KEPT, SECRET_THANKS])
            await asyncio.wait_for(task, 60)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], self.store.events(key)[-8:]))
        # The code got to the portal, once, and the ticket was billed after it.
        self.assertEqual(portal.codes, [CODE])
        self.assertEqual(len(portal.emitted), 1)
        events = self.store.events(key)
        self.assertIn('Se escribió en el portal el código que llegó a tu teléfono.',
                      [event['detail'] for event in events if event['kind'] == 'portal_account'])
        self.assertNotIn('needs_user', kinds(self.store, key))
        # It is in nothing that was stored, nor in anything either model was shown.
        plan.infer.assert_not_called()
        files = b''.join(path.read_bytes() for pattern in ('facturador.sqlite*', 'memoria_portales.sqlite*', 'auth/**/*.json')
                         for path in self.store.root.glob(pattern)).decode('latin-1')
        everything = json.dumps([events, self.store.chat_messages(), brain.calls, files], ensure_ascii=False, default=str)
        self.assertNotIn(CODE.casefold(), everything.casefold())
        self.assertEqual(len(brain.calls), 4)
        self.assertEqual((self.runner.asked, self.runner.live_notes), ({}, {}))


if __name__ == '__main__':
    unittest.main()
