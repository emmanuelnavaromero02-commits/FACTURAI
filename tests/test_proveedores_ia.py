"""Proveedores de IA por llave y selector: sin red, con un transporte httpx simulado."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from facturador import uso_ia
from facturador.auth import ChatGPTPlan, PlanError
from facturador.ia_ajustes import AjustesIA, ia_router
from facturador.models import Action, Extraction, Plan, PortalSearch, RegistrationPlan
from facturador.proveedores_ia import (ANTHROPIC_API, ANTHROPIC_DEFAULT_MODEL, ANTHROPIC_VERSION, OPENAI_API,
                                       AnthropicKeyProvider, OpenAIKeyProvider, SelectorIA, anthropic_content,
                                       anthropic_schema)

KEY = 'sk-ant-api03-PRUEBA-ficticia-0000000000000000wxyz'
OPENAI_KEY = 'sk-proj-PRUEBA-ficticia-000000000000000000abcd'
REPLY = {'is_receipt': True, 'provider': 'capufe', 'merchant': 'CAPUFE', 'amount': '50.00', 'date': '2026-11-21',
         'ticket_number': '0000042', 'billing_codes': [], 'billing_url': None, 'payment_code': '01',
         'payment_evidence': 'Pago en efectivo', 'details': '', 'uncertainties': [], 'issuer_rfc': None,
         'merchant_whatsapp': None, 'merchant_email': None}
IMAGE = {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,/9j/AAAA', 'detail': 'high'}
TEXT = {'type': 'input_text', 'text': 'Ticket de prueba'}


def message(payload, *, stop='end_turn', usage=None, content=None, model=ANTHROPIC_DEFAULT_MODEL):
    blocks = content if content is not None else [{'type': 'thinking', 'thinking': '', 'signature': 'x'},
                                                    {'type': 'text', 'text': json.dumps(payload)}]
    return httpx.Response(200, json={'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': model,
                                     'content': blocks, 'stop_reason': stop,
                                     'usage': usage or {'input_tokens': 1200, 'output_tokens': 80}})


def api_error(status, kind, text='', headers=None, details=None):
    error = {'type': kind, 'message': text}
    if details:
        error['details'] = details
    return httpx.Response(status, json={'type': 'error', 'error': error}, headers=headers or {})


class Fake:
    """Queue of canned responses; records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply(request) if callable(reply) else reply

    def body(self, index=-1):
        return json.loads(self.requests[index].content)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environ = {}
        self.settings = AjustesIA(self.root, self.environ)
        sleep = patch('facturador.proveedores_ia.time.sleep')
        self.slept = sleep.start()
        self.addCleanup(sleep.stop)

    def provider(self, cls, fake, key=KEY):
        if key:
            self.settings.save_key(cls.name, key)
        result = cls(self.settings, httpx.Client(transport=httpx.MockTransport(fake)))
        self.addCleanup(result.close)
        return result


class AnthropicTests(Base):
    def test_request_converts_text_image_schema_and_level(self):
        fake = Fake(message(REPLY))
        claude = self.provider(AnthropicKeyProvider, fake)
        result, usage = claude.infer_measured('Extrae el folio', [IMAGE, TEXT], Extraction, effort='low')
        self.assertEqual(result.ticket_number, '0000042')
        self.assertEqual(usage, {'model': ANTHROPIC_DEFAULT_MODEL, 'input_tokens': 1200, 'output_tokens': 80})
        request = fake.requests[0]
        self.assertEqual(str(request.url), ANTHROPIC_API + '/messages')
        self.assertEqual(request.headers['x-api-key'], KEY)
        self.assertEqual(request.headers['anthropic-version'], ANTHROPIC_VERSION)
        self.assertEqual(request.headers['anthropic-beta'], 'server-side-fallback-2026-07-01')
        body = fake.body()
        self.assertEqual(body['model'], 'claude-opus-5-5')
        self.assertEqual(body['system'], 'Extrae el folio')
        self.assertEqual(body['fallbacks'], 'default')
        self.assertNotIn('thinking', body)
        self.assertNotIn('temperature', body)
        self.assertEqual(body['messages'], [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': '/9j/AAAA'}},
            {'type': 'text', 'text': 'Ticket de prueba'}]}])
        output = body['output_config']
        self.assertEqual(output['effort'], 'low')
        schema = output['format']['schema']
        self.assertEqual(output['format']['type'], 'json_schema')
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']), set(Extraction.model_fields))
        self.assertNotIn('tools', body)

    def test_levels_map_to_effort_and_owner_tier_models(self):
        self.settings.save_key('anthropic', KEY)
        self.settings.set_models('anthropic', {'rapido': 'claude-haiku-5-5', 'dificil': 'claude-fable-5-2'})
        fake = Fake(*[message(REPLY) for _ in range(4)])
        claude = self.provider(AnthropicKeyProvider, fake, key='')
        for effort in ('low', 'medium', 'high', 'max'):
            claude.infer('x', [TEXT], Extraction, effort=effort)
        self.assertEqual([(b['model'], b['output_config']['effort']) for b in map(fake.body, range(4))],
                         [('claude-haiku-5-5', 'low'), ('claude-opus-5-5', 'medium'),
                          ('claude-fable-5-2', 'high'), ('claude-fable-5-2', 'max')])
        self.assertNotIn('fallbacks', fake.body(0))  # Haiku has no server-side fallback.
        self.assertNotIn('anthropic-beta', fake.requests[0].headers)

    def test_schema_drops_keywords_claude_rejects_but_pydantic_still_checks(self):
        from pydantic import BaseModel, ConfigDict, Field

        class Limited(BaseModel):
            model_config = ConfigDict(extra='forbid')
            pattern: str = Field(max_length=4)  # A property called «pattern» must survive.
            items: list[str] = Field(min_length=1)

        schema = anthropic_schema(Limited)
        self.assertIn('pattern', schema['properties'])
        self.assertNotIn('maxLength', json.dumps(schema))
        self.assertNotIn('minItems', json.dumps(schema))
        fake = Fake(message({'pattern': 'demasiado', 'items': ['a']}))
        claude = self.provider(AnthropicKeyProvider, fake)
        with self.assertRaises(PlanError):
            claude.infer('x', [TEXT], Limited)

    def test_web_search_returns_sources_like_today_and_resumes_a_paused_turn(self):
        search = [{'type': 'server_tool_use', 'id': 'srv_1', 'name': 'web_search', 'input': {'query': 'facturación'}},
                  {'type': 'web_search_tool_result', 'tool_use_id': 'srv_1', 'content': [
                      {'type': 'web_search_result', 'url': 'https://cafeteria.example.org/facturacion', 'title': 'T'},
                      {'type': 'web_search_result', 'url': 'https://otro.example.org/', 'title': 'U'}]}]
        answer = {'billing_url': 'https://facturas.example.org/emitir',
                  'evidence_url': 'https://cafeteria.example.org/facturacion'}
        fake = Fake(message(None, stop='pause_turn', content=search),
                    message(None, content=[{'type': 'text', 'text': 'Encontré el portal.', 'citations': [
                        {'type': 'web_search_result_location', 'url': 'https://cafeteria.example.org/facturacion'}]},
                        {'type': 'server_tool_use', 'id': 'srv_2', 'name': 'web_search', 'input': {}},
                        {'type': 'web_search_tool_result', 'tool_use_id': 'srv_2', 'content': []},
                        {'type': 'text', 'text': json.dumps(answer)}]))
        claude = self.provider(AnthropicKeyProvider, fake)
        (found, sources), usage = claude.infer_measured('Busca', [TEXT], PortalSearch, effort='medium', web_search=True)
        self.assertEqual(found.billing_url, answer['billing_url'])
        self.assertEqual(sources, ['https://cafeteria.example.org/facturacion', 'https://otro.example.org/'])
        self.assertEqual(fake.body(0)['tools'], [{'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 5}])
        resumed = fake.body(1)['messages']
        self.assertEqual([m['role'] for m in resumed], ['user', 'assistant'])
        self.assertEqual(resumed[1]['content'], search)
        self.assertEqual((usage['input_tokens'], usage['output_tokens']), (2400, 160))

    def test_older_model_gets_basic_search_tool(self):
        self.settings.save_key('anthropic', KEY)
        self.settings.set_models('anthropic', {'normal': 'claude-haiku-4-5'})
        fake = Fake(message({'answer': 'ok'}))
        from facturador.models import WebAnswer
        claude = self.provider(AnthropicKeyProvider, fake, key='')
        claude.infer('x', [TEXT], WebAnswer, web_search=True)
        self.assertEqual(fake.body()['tools'][0]['type'], 'web_search_20250305')

    def test_refused_level_falls_back_once_and_is_not_asked_again(self):
        fake = Fake(api_error(400, 'invalid_request_error', 'output_config.effort: not supported'),
                    message(REPLY), message(REPLY))
        claude = self.provider(AnthropicKeyProvider, fake)
        claude.infer('x', [TEXT], Extraction, effort='max')
        claude.infer('x', [TEXT], Extraction, effort='max')
        self.assertEqual(fake.body(0)['output_config']['effort'], 'max')
        self.assertNotIn('effort', fake.body(1)['output_config'])
        self.assertNotIn('effort', fake.body(2)['output_config'])

    def test_account_without_refusal_fallback_asks_without_it(self):
        fake = Fake(api_error(400, 'invalid_request_error', 'Unexpected value(s) for the `anthropic-beta` header.'),
                    message(REPLY), message(REPLY))
        claude = self.provider(AnthropicKeyProvider, fake)
        claude.infer('x', [TEXT], Extraction)
        claude.infer('x', [TEXT], Extraction)
        self.assertIn('fallbacks', fake.body(0))
        self.assertNotIn('fallbacks', fake.body(1))
        self.assertNotIn('anthropic-beta', fake.requests[2].headers)

    def assert_error(self, response, status, retry_after=0):
        claude = self.provider(AnthropicKeyProvider, Fake(response, response))
        with self.assertRaises(PlanError) as raised:
            claude.infer('x', [TEXT], Extraction)
        self.assertEqual((raised.exception.status, raised.exception.retry_after), (status, retry_after))
        self.assertNotIn(KEY, str(raised.exception))
        return claude, raised.exception

    def test_each_error_becomes_the_states_the_program_understands(self):
        claude, _ = self.assert_error(api_error(401, 'authentication_error', 'invalid x-api-key'), 'reconnect')
        self.assertEqual(claude.status()['status'], 'reconnect')
        claude, _ = self.assert_error(api_error(402, 'billing_error', 'credit balance too low'), 'limit')
        self.assertEqual(claude.status()['status'], 'limit')
        claude.clear_limit()
        self.assertEqual(claude.status()['status'], 'ready')
        self.assert_error(api_error(429, 'rate_limit_error', 'spend cap',
                                    details={'error_code': 'enforced_spend_limit_reached'}), 'limit')
        self.assert_error(api_error(429, 'rate_limit_error', 'slow down', headers={'retry-after': '30'}),
                          'transient', 30)
        self.assert_error(api_error(529, 'overloaded_error', 'Overloaded'), 'transient')
        self.assert_error(api_error(500, 'api_error', 'boom'), 'transient')
        self.assert_error(api_error(403, 'permission_error', 'no'), 'ineligible')
        self.assert_error(api_error(404, 'not_found_error', 'model: claude-nada'), 'model')
        self.assert_error(api_error(400, 'invalid_request_error', 'prompt is too long'), 'model')
        self.assert_error(message(None, stop='refusal', content=[]), 'unavailable')
        self.assert_error(message(None, stop='max_tokens', content=[{'type': 'text', 'text': '{"a'}]), 'unavailable')
        self.assert_error(message(None, content=[{'type': 'text', 'text': 'no es json'}]), 'unavailable')
        self.assert_error(httpx.ConnectError('caído'), 'transient')

    def test_short_transient_wait_retries_once_like_the_plan(self):
        fake = Fake(api_error(529, 'overloaded_error', 'Overloaded'), message(REPLY))
        claude = self.provider(AnthropicKeyProvider, fake)
        self.assertEqual(claude.infer('x', [TEXT], Extraction).ticket_number, '0000042')
        self.assertEqual(len(fake.requests), 2)
        self.slept.assert_called_once_with(0.5)

    def test_new_key_lifts_the_rejection_of_the_old_one(self):
        claude, _ = self.assert_error(api_error(401, 'authentication_error', ''), 'reconnect')
        self.settings.save_key('anthropic', KEY.replace('wxyz', 'nuev'))
        self.assertEqual(claude.status()['status'], 'ready')

    def test_lists_models_across_pages(self):
        fake = Fake(httpx.Response(200, json={'data': [{'id': 'claude-opus-5-5', 'display_name': 'Claude Opus 5.5'}],
                                              'has_more': True, 'last_id': 'claude-opus-5-5'}),
                    httpx.Response(200, json={'data': [{'id': 'claude-haiku-5-5', 'display_name': 'Claude Haiku 5.5'}],
                                              'has_more': False, 'last_id': 'claude-haiku-5-5'}))
        claude = self.provider(AnthropicKeyProvider, fake)
        self.assertEqual([m['id'] for m in claude.models()], ['claude-opus-5-5', 'claude-haiku-5-5'])
        self.assertEqual(fake.requests[1].url.params['after_id'], 'claude-opus-5-5')
        claude.models()
        self.assertEqual(len(fake.requests), 2)  # Cached for a while.

    def test_content_conversion_rejects_what_claude_cannot_take(self):
        with self.assertRaises(PlanError):
            anthropic_content([{'type': 'input_image', 'image_url': 'data:image/bmp;base64,AAAA'}])
        with self.assertRaises(PlanError):
            anthropic_content([{'type': 'input_file', 'file_id': 'x'}])
        self.assertEqual(anthropic_content([{'type': 'input_text', 'text': ''}, TEXT]), [{'type': 'text', 'text': 'Ticket de prueba'}])

    def test_without_key_it_is_disconnected(self):
        claude = AnthropicKeyProvider(self.settings, httpx.Client(transport=httpx.MockTransport(Fake())))
        self.addCleanup(claude.close)
        self.assertEqual(claude.status()['status'], 'disconnected')
        with self.assertRaises(PlanError) as raised:
            claude.infer('x', [TEXT], Extraction)
        self.assertEqual(raised.exception.status, 'disconnected')


def sse(*events):
    text = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events)
    return httpx.Response(200, text=text, headers={'Content-Type': 'text/event-stream'})


def completed(payload, extra=(), usage=None):
    output = [*extra, {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(payload)}]}]
    return {'type': 'response.completed', 'response': {'output': output,
                                                       'usage': usage or {'input_tokens': 900, 'output_tokens': 50}}}


class OpenAITests(Base):
    def test_same_request_as_the_plan_with_the_product_key(self):
        fake = Fake(sse(completed(REPLY)))
        gpt = self.provider(OpenAIKeyProvider, fake, OPENAI_KEY)
        result, usage = gpt.infer_measured('Extrae', [IMAGE, TEXT], Extraction, effort='high')
        self.assertEqual(result.ticket_number, '0000042')
        self.assertEqual(usage['input_tokens'], 900)
        request = fake.requests[0]
        self.assertEqual(str(request.url), OPENAI_API + '/responses')
        self.assertEqual(request.headers['Authorization'], 'Bearer ' + OPENAI_KEY)
        body = fake.body()
        self.assertEqual((body['stream'], body['store'], body['reasoning']), (True, False, {'effort': 'high'}))
        self.assertTrue(body['text']['format']['strict'])
        self.assertEqual(body['input'], [{'role': 'user', 'content': [IMAGE, TEXT]}])
        self.assertEqual(body['instructions'], 'Extrae')

    def test_web_search_sources(self):
        call = {'type': 'web_search_call', 'action': {'sources': [{'url': 'https://cafeteria.example.org/facturacion'}]}}
        fake = Fake(sse(completed({'billing_url': None, 'evidence_url': None}, extra=[call])))
        gpt = self.provider(OpenAIKeyProvider, fake, OPENAI_KEY)
        found, sources = gpt.infer('Busca', [TEXT], PortalSearch, web_search=True)
        self.assertEqual(sources, ['https://cafeteria.example.org/facturacion'])
        self.assertEqual(fake.body()['tools'], [{'type': 'web_search', 'search_context_size': 'low'}])

    def test_refused_level_falls_back_to_medium(self):
        fake = Fake(httpx.Response(400, json={'error': {'code': 'unsupported_value', 'param': 'reasoning.effort',
                                                        'message': 'x'}}), sse(completed(REPLY)))
        gpt = self.provider(OpenAIKeyProvider, fake, OPENAI_KEY)
        gpt.infer('x', [TEXT], Extraction, effort='high')
        self.assertEqual(fake.body(1)['reasoning'], {'effort': 'medium'})

    def test_errors(self):
        cases = [(httpx.Response(401, json={'error': {'code': 'invalid_api_key'}}), 'reconnect', 0),
                 (httpx.Response(429, json={'error': {'code': 'insufficient_quota'}}), 'limit', 0),
                 (httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}}, headers={'retry-after': '20'}),
                  'transient', 20),
                 (httpx.Response(404, json={'error': {'code': 'model_not_found'}}), 'model', 0),
                 (httpx.Response(403, json={'error': {}}), 'ineligible', 0),
                 (sse({'type': 'response.failed', 'response': {'error': {'code': 'server_error'}}}), 'transient', 0)]
        for response, status, wait in cases:
            gpt = self.provider(OpenAIKeyProvider, Fake(response, response), OPENAI_KEY)
            with self.assertRaises(PlanError) as raised:
                gpt.infer('x', [TEXT], Extraction)
            self.assertEqual((raised.exception.status, raised.exception.retry_after), (status, wait), status)
            self.assertNotIn(OPENAI_KEY, str(raised.exception))


class SelectorTests(Base):
    def selector(self, fake=None, chatgpt=None):
        client = httpx.Client(transport=httpx.MockTransport(fake or Fake()))
        result = SelectorIA(self.root, chatgpt=chatgpt, client=client, environ=self.environ)
        self.addCleanup(result.close)
        return result

    def usage_rows(self):
        db = sqlite3.connect(self.root / 'facturador.sqlite')
        try:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ai_usage ORDER BY id')]
        finally:
            db.close()

    def test_without_configuration_it_is_exactly_the_chatgpt_plan(self):
        real = ChatGPTPlan(self.root)
        self.addCleanup(real.close)
        plain = SelectorIA(self.root, environ={})
        self.addCleanup(plain.close)
        self.assertIsInstance(plain.chatgpt, ChatGPTPlan)
        self.assertEqual(plain.status(), real.status())
        self.assertEqual(plain.session_path, real.session_path)

        plan = Mock(spec=ChatGPTPlan)
        plan.status.return_value = {'status': 'ready', 'email': '', 'model': 'gpt-6.1-sol', 'message': '', 'registered': True}
        plan.infer.return_value = 'resultado'
        selector = self.selector(chatgpt=plan)
        self.assertEqual(selector.infer('i', [TEXT], Extraction, effort='low'), 'resultado')
        plan.infer.assert_called_once_with('i', [TEXT], Extraction, effort='low', web_search=False)
        self.assertEqual(selector.status(), plan.status.return_value)
        selector.start(True, False)
        plan.start.assert_called_once_with(True, False)
        selector.finish('s', 'c', 'oaiapp_x', '')
        plan.finish.assert_called_once_with('s', 'c', 'oaiapp_x', '')
        selector.disconnect()
        plan.disconnect.assert_called_once_with()
        selector.clear_limit()
        plan.clear_limit.assert_called_once_with()
        selector.models()
        plan.models.assert_called_once_with()
        selector.select_model('gpt-6.1-sol')
        plan.select_model.assert_called_once_with('gpt-6.1-sol')
        plan.infer.side_effect = PlanError('ocupado', 'transient', 3)
        with self.assertRaises(PlanError) as raised:
            selector.infer('i', [TEXT], Action)
        self.assertEqual(raised.exception.retry_after, 3)
        rows = self.usage_rows()
        self.assertEqual([(r['provider'], r['task'], r['outcome'], r['model']) for r in rows],
                         [('chatgpt', 'leer_ticket', 'ok', 'gpt-6.1-sol'), ('chatgpt', 'paso_portal', 'transient', 'gpt-6.1-sol')])
        self.assertIsNone(rows[0]['input_tokens'])

    def test_delegates_to_the_chosen_key_provider_and_logs_tokens(self):
        plan = Mock(spec=ChatGPTPlan)
        fake = Fake(message(REPLY, usage={'input_tokens': 1000, 'cache_read_input_tokens': 500, 'output_tokens': 40}))
        selector = self.selector(fake, plan)
        self.settings.save_key('anthropic', KEY)
        self.settings.choose('anthropic')
        self.assertEqual(selector.status()['status'], 'ready')
        with uso_ia.en_ticket('ticket-1'):
            self.assertEqual(selector.infer('i', [TEXT], Extraction, effort='medium').amount, '50.00')
        plan.infer.assert_not_called()
        row = self.usage_rows()[0]
        self.assertEqual((row['provider'], row['model'], row['ticket'], row['task'], row['effort']),
                         ('anthropic', 'claude-opus-5-5', 'ticket-1', 'leer_ticket', 'medium'))
        self.assertEqual((row['input_tokens'], row['output_tokens'], row['outcome']), (1500, 40, 'ok'))
        # OAuth buttons keep acting on the ChatGPT plan.
        selector.disconnect()
        plan.disconnect.assert_called_once_with()

    def test_removing_the_active_key_returns_to_the_plan(self):
        selector = self.selector(chatgpt=Mock(spec=ChatGPTPlan))
        self.settings.save_key('openai', OPENAI_KEY)
        self.settings.choose('openai')
        self.assertEqual(selector.active_name, 'openai')
        self.settings.remove_key('openai')
        self.assertEqual(selector.active_name, 'chatgpt')
        with self.assertRaises(ValueError):
            self.settings.choose('anthropic')  # No key saved.


class ContextAndCostTests(Base):
    def test_task_and_ticket_by_context_with_threads_and_default(self):
        registro = uso_ia.RegistroUso(self.root)
        self.assertEqual(uso_ia.tarea_actual(Extraction), ('leer_ticket', ''))
        self.assertEqual(uso_ia.tarea_actual(object), ('otra', ''))

        async def flow():
            with uso_ia.en_tarea('chat', 'abc'):
                return await asyncio.to_thread(uso_ia.tarea_actual, Extraction)
        self.assertEqual(asyncio.run(flow()), ('chat', 'abc'))
        with uso_ia.en_ticket('k-9'):
            self.assertEqual(uso_ia.tarea_actual(PortalSearch), ('buscar_portal', 'k-9'))
            with uso_ia.en_tarea('whatsapp'):
                self.assertEqual(uso_ia.tarea_actual(Action), ('whatsapp', 'k-9'))
        self.assertEqual(uso_ia.tarea_actual(Action), ('paso_portal', ''))
        # A whole-screen plan and the plan of a registration screen are steps of the portal too.
        self.assertEqual(uso_ia.tarea_actual(Plan), ('paso_portal', ''))
        self.assertEqual(uso_ia.tarea_actual(RegistrationPlan), ('paso_portal', ''))
        with uso_ia.en_ticket('../../no válido'):
            self.assertEqual(uso_ia.tarea_actual(Action)[1], '')
        registro.record(task='paso_portal', ticket='k', provider='anthropic', model='claude-opus-5-5', effort='high',
                        duration_ms=12, input_tokens=2_000_000, output_tokens=100_000, outcome='ok')
        registro.record(task='chat', ticket='', provider='chatgpt', model='gpt-6.1-sol', effort='low', duration_ms=3)
        self.assertEqual([g['costo'] for g in uso_ia.resumen_uso(registro)['grupos']], ['sin precio', 'sin precio'])
        uso_ia.guardar_precios(self.root, {'moneda': 'USD', 'precios': {
            'anthropic': {'claude-opus-5-5': {'entrada': 4, 'salida': 20}}}})
        groups = {g['proveedor']: g for g in uso_ia.resumen_uso(registro)['grupos']}
        self.assertEqual(groups['anthropic']['costo'], 10.0)  # 2M × 4 + 0.1M × 20
        self.assertEqual(groups['chatgpt']['costo'], 'sin precio')  # No tokens reported by the plan.
        self.assertIsNone(groups['chatgpt']['tokens_entrada'])
        with self.assertRaises(ValueError):
            uso_ia.guardar_precios(self.root, {'precios': {'anthropic': {'m': {'entrada': -1, 'salida': 1}}}})

    def test_recording_never_breaks_a_call(self):
        registro = uso_ia.RegistroUso(self.root / 'no-existe' / 'x')
        (self.root / 'no-existe').write_text('archivo, no carpeta')
        registro.record(task='chat', ticket='', provider='chatgpt', model='', effort='low', duration_ms=1)
        self.assertEqual(registro.rows(), [])


class KeySecrecyTests(Base):
    def app(self, fake):
        selector = SelectorIA(self.root, chatgpt=Mock(spec=ChatGPTPlan, **{'status.return_value': {'status': 'ready'}}),
                              client=httpx.Client(transport=httpx.MockTransport(fake)), environ=self.environ)
        self.addCleanup(selector.close)
        app = FastAPI()

        @app.exception_handler(ValueError)
        async def invalid(request, exc):
            return PlainTextResponse(str(exc), status_code=422)

        @app.exception_handler(KeyError)
        async def missing(request, exc):
            return PlainTextResponse('Registro inexistente', status_code=404)
        app.include_router(ia_router(self.root, selector))
        return TestClient(app), selector

    def disk_text(self):
        return b''.join(p.read_bytes() for p in self.root.rglob('*') if p.is_file())

    def test_key_is_saved_encrypted_tested_and_never_returned(self):
        models = httpx.Response(200, json={'data': [{'id': 'claude-opus-5-5', 'display_name': 'Claude Opus 5.5'},
                                                    {'id': 'claude-haiku-5-5', 'display_name': 'Claude Haiku 5.5'}],
                                           'has_more': False})
        fake = Fake(api_error(401, 'authentication_error', 'invalid x-api-key ' + KEY), models, models,
                    message(REPLY))
        client, selector = self.app(fake)
        rejected = client.put('/api/ia/llave/anthropic', json={'llave': KEY})
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn(KEY, rejected.text)
        self.assertFalse(self.settings.key_info('anthropic')['configurada'])
        self.assertEqual(fake.requests[0].headers['x-api-key'], KEY)  # The probe used the key typed.

        saved = client.put('/api/ia/llave/anthropic', json={'llave': KEY})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn(KEY, saved.text)
        entry = {p['id']: p for p in saved.json()['proveedores']}['anthropic']
        self.assertEqual(entry['llave'], {'configurada': True, 'ultimos': 'wxyz', 'origen': 'panel', 'editable': True,
                                          'guardada': entry['llave']['guardada']})
        self.assertNotIn(KEY.encode(), self.disk_text())
        self.assertNotIn(KEY, client.get('/api/ia').text)

        self.assertEqual(client.put('/api/ia/proveedor', json={'proveedor': 'anthropic'}).json()['activo'], 'anthropic')
        listed = client.get('/api/ia/modelos/anthropic').json()
        self.assertEqual([m['id'] for m in listed['modelos']], ['claude-opus-5-5', 'claude-haiku-5-5'])
        chosen = client.put('/api/ia/modelos/anthropic', json={'rapido': 'claude-haiku-5-5'})
        self.assertEqual(chosen.status_code, 200, chosen.text)
        self.assertEqual(selector.providers['anthropic'].tier_models()['rapido'], 'claude-haiku-5-5')
        self.assertEqual(client.put('/api/ia/modelos/anthropic', json={'normal': 'gpt-inventado'}).status_code, 422)

        with uso_ia.en_ticket('t-1'):
            selector.infer('x', [TEXT], Extraction, effort='low')
        self.assertEqual(fake.body()['model'], 'claude-haiku-5-5')
        self.assertNotIn(KEY.encode(), self.disk_text())  # Usage rows and settings alike.

        removed = client.delete('/api/ia/llave/anthropic').json()
        self.assertEqual(removed['activo'], 'chatgpt')
        self.assertFalse({p['id']: p for p in removed['proveedores']}['anthropic']['llave']['configurada'])

    def test_bad_body_is_never_echoed(self):
        client, _ = self.app(Fake())
        for body in ({'llave': KEY, 'otra': 1}, {'llave': KEY + ' con espacio'}, {'clave': KEY}):
            response = client.put('/api/ia/llave/anthropic', json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(KEY, response.text)

    def test_environment_key_wins_and_cannot_be_changed(self):
        self.environ['FACTURAI_ANTHROPIC_API_KEY'] = KEY
        client, selector = self.app(Fake(message(REPLY)))
        status = client.get('/api/ia').json()
        entry = {p['id']: p for p in status['proveedores']}['anthropic']
        self.assertEqual(status['activo'], 'anthropic')
        self.assertEqual(entry['llave'], {'configurada': True, 'ultimos': 'wxyz', 'origen': 'entorno', 'editable': False})
        self.assertEqual(client.put('/api/ia/llave/anthropic', json={'llave': OPENAI_KEY}).status_code, 422)
        self.assertEqual(client.delete('/api/ia/llave/anthropic').status_code, 422)
        self.assertEqual(self.settings.key('anthropic'), KEY)
        self.assertNotIn(KEY, client.get('/api/ia').text)
        selector.infer('x', [TEXT], Extraction)
        self.assertNotIn(KEY.encode(), self.disk_text())
        # The owner can still pick the plan explicitly.
        self.assertEqual(client.put('/api/ia/proveedor', json={'proveedor': 'chatgpt'}).json()['activo'], 'chatgpt')

    def test_prices_route(self):
        client, _ = self.app(Fake())
        self.assertEqual(client.get('/api/ia/precios').json(), {'moneda': 'USD', 'precios': {}})
        table = {'moneda': 'MXN', 'precios': {'openai': {'gpt-6.1-sol': {'entrada': 1.5, 'salida': 6}}}}
        self.assertEqual(client.put('/api/ia/precios', json=table).json()['moneda'], 'MXN')
        self.assertEqual(client.put('/api/ia/precios', content=b'{no json').status_code, 422)
        self.assertEqual(client.get('/api/ia/uso?periodo=todo').json()['moneda'], 'MXN')


if __name__ == '__main__':
    unittest.main()
