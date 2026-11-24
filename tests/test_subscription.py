from __future__ import annotations

import base64
import hashlib
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from facturador.ai import AI
from facturador.auth import AUTHORIZATION, ISSUER, JWKS, PLAN_SCOPES, RESOURCE, TOKEN, ChatGPTPlan, PlanError, read_json, write_private
from facturador.models import Extraction, PortalSearch


REPLY = {'is_receipt': True, 'provider': 'capufe', 'merchant': 'CAPUFE', 'amount': '50.00', 'date': '2026-11-21',
         'ticket_number': '0000042', 'billing_codes': [], 'billing_url': None,
         'payment_code': '01', 'payment_evidence': 'Pago en efectivo', 'details': '', 'uncertainties': []}


class SubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private.public_key()))
        cls.jwk.update(kid='test-jwk', alg='RS256')

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.traffic = []
        self.transports = []
        self.plan = self.new_plan(self.fake_openai)
        self.auth_attempt = parse_qs(urlsplit(self.plan.start()).query)
        self.scopes = 'openid email profile offline_access ' + ' '.join(sorted(PLAN_SCOPES))
        self.served_client = 'oaiapp_example'

    def new_plan(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = ChatGPTPlan(Path(self.tmp.name), client=client)
        self.addCleanup(result.close)
        return result

    def fake_openai(self, request):
        self.traffic.append(request)
        if str(request.url) == TOKEN:
            form = parse_qs(request.content.decode())
            assert form['client_id'] == [self.served_client]
            assert form['resource'] == [RESOURCE]
            if form['grant_type'] == ['refresh_token']:
                assert form['refresh_token'] == ['old-refresh']
                return httpx.Response(200, json={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'scope': self.scopes, 'expires_in': 3600})
            assert form['redirect_uri'] == self.auth_attempt['redirect_uri']
            challenge = hashlib.sha256(form['code_verifier'][0].encode()).digest()
            assert base64.urlsafe_b64encode(challenge).decode().rstrip('=') == self.auth_attempt['code_challenge'][0]
            auth = jwt.encode({'iss': ISSUER, 'aud': self.served_client, 'sub': 'local-test', 'nonce': self.auth_attempt['nonce'][0],
                               'email': 'demo@example.test', 'iat': int(time.time()), 'exp': int(time.time()) + 300},
                              self.private, algorithm='RS256', headers={'kid': self.jwk['kid']})
            return httpx.Response(200, json={'access_token': 'old-access', 'refresh_token': 'old-refresh', 'id_token': auth,
                                             'scope': self.scopes, 'expires_in': 3600})
        if str(request.url) == JWKS:
            return httpx.Response(200, json={'keys': [self.jwk]})
        if str(request.url) == RESOURCE + '/responses':
            assert request.headers['Authorization'] in {'Bearer new-access', 'Bearer old-access'}
            record = json.loads(request.content)
            assert record['stream'] is True and record['store'] is False
            assert record['text']['format']['strict'] and record['model'] == 'gpt-6.1-sol'
            assert record['reasoning'] == {'effort': 'max'}
            assert 'previous_response_id' not in record
            data = {'type': 'response.completed', 'response': {'output': [{'type': 'message',
                    'content': [{'type': 'output_text', 'text': json.dumps(REPLY)}]}]}}
            payload = 'event: response.completed\ndata: ' + json.dumps(data) + '\n\n'
            return httpx.Response(200, text=payload, headers={'Content-Type': 'text/event-stream'})
        raise AssertionError(str(request.url))

    def finish(self):
        self.plan.finish(self.auth_attempt['state'][0], 'synthetic-code', self.served_client)

    def test_registered_session_reads_structured_ticket_without_api_key(self):
        self.assertEqual(self.auth_attempt['client_id'], ['dynamic_agent_client'])
        self.assertEqual(urlsplit(AUTHORIZATION).hostname, 'auth.openai.com')
        self.assertTrue(self.auth_attempt['redirect_uri'][0].startswith('http://127.0.0.1:'))
        self.finish()
        result = self.plan.infer('Extrae el folio', [{'type': 'input_text', 'text': 'Ejemplo'}], Extraction)
        self.assertEqual(result.ticket_number, '0000042')
        self.assertEqual(self.plan.status()['status'], 'ready')
        self.assertNotIn('access_token', self.plan.status())
        login = parse_qs(urlsplit(self.plan.start()).query)
        self.assertEqual(login['client_id'], [self.served_client])
        self.assertEqual(login['ext_agent_host_id'], self.auth_attempt['ext_agent_host_id'])
        self.assertNotEqual(login['state'], self.auth_attempt['state'])

    def test_all_ai_tasks_request_max_reasoning(self):
        source = Path(self.tmp.name) / 'sample.txt'
        source.write_text('Ticket de prueba', encoding='utf-8')
        plan = Mock()
        ai = AI(plan)
        ai.extract(source, {})
        ai.tax_profile(source)
        ai.next_action('Instrucciones', '{}', None)
        evidence = 'https://cafeteria.example.org/facturacion'
        plan.infer.return_value = (PortalSearch(billing_url='https://facturas.example.org/emitir',
                                               evidence_url=evidence), [evidence])
        ai.discover_portal('Cafetería')
        self.assertEqual([call.kwargs.get('effort', 'max') for call in plan.infer.call_args_list],
                         ['max', 'max', 'max', 'max'])

    def test_callback_rejects_wrong_state_and_validation_precedes_inference(self):
        with self.assertRaises(PlanError):
            self.plan.finish('bogus-state', 'synthetic-code', self.served_client)
        self.assertEqual(self.traffic, [])
        self.assertEqual(self.plan.status()['status'], 'disconnected')
        self.scopes = 'email openid profile'
        self.finish()
        with self.assertRaises(PlanError):
            self.plan.infer('Prueba', [], Extraction)
        self.assertEqual(self.plan.status()['status'], 'identity_only')
        self.assertEqual(len(self.traffic), 2)

    def test_expired_session_rotates_refresh_and_responses_uses_replacement(self):
        self.finish()
        record = read_json(self.plan.session_path)
        record['expires_at'] = time.time() - 10
        write_private(self.plan.session_path, record)
        self.plan.infer('Prueba de renovación', [], Extraction)
        self.assertEqual(self.traffic[-1].headers['Authorization'], 'Bearer new-access')
        self.assertEqual(read_json(self.plan.session_path)['refresh_token'], 'new-refresh')

    def test_limit_preserves_session_and_stops_another_request(self):
        self.finish()
        hit = []

        def limited(request):
            hit.append(request)
            return httpx.Response(429, json={'error': {'code': 'subscription_sharing_usage_limit_exceeded'}})

        blocked = self.new_plan(limited)
        with self.assertRaises(PlanError):
            blocked.infer('Prueba de límite', [], Extraction)
        self.assertEqual(blocked.status()['status'], 'limit')
        with self.assertRaises(PlanError):
            blocked.infer('Prueba de pausa', [], Extraction)
        self.assertEqual(len(hit), 1)
        blocked.clear_limit()
        self.assertEqual(blocked.status()['status'], 'ready')

    def test_retry_after_for_service_throttling_keeps_the_plan_ready(self):
        self.finish()
        for status in (429, 503):
            with self.subTest(status=status):
                plan = self.new_plan(lambda _: httpx.Response(status, headers={'Retry-After': '80'},
                                                               json={'error': {'code': 'rate_limit_exceeded'}}))
                with self.assertRaises(PlanError) as result:
                    plan.infer('Lectura', [], Extraction)
                self.assertEqual((result.exception.status, result.exception.retry_after), ('transient', 80))
                self.assertEqual(plan.status()['status'], 'ready')

    def test_failed_stream_retries_inference_once_without_exposing_response_text(self):
        self.finish()
        attempts = []
        failed = {'type': 'response.failed', 'response': {
            'error': {'code': 'model_backend_failure', 'message': 'private portal content'}}}
        complete = {'type': 'response.completed', 'response': {'output': [{'type': 'message',
            'content': [{'type': 'output_text', 'text': json.dumps(REPLY)}]}]}}

        def handler(request):
            attempts.append(request)
            event = failed if len(attempts) == 1 else complete
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n',
                                  headers={'Content-Type': 'text/event-stream'})

        plan = self.new_plan(handler)
        with patch('facturador.auth.time.sleep') as pause:
            result = plan.infer('Lee este ticket', [{'type': 'input_text', 'text': 'datos'}], Extraction)
        self.assertEqual(result.ticket_number, REPLY['ticket_number'])
        self.assertEqual(len(attempts), 2)
        pause.assert_called_once_with(0.5)
        self.assertEqual(attempts[0].content, attempts[1].content)

    def test_repeated_incomplete_stream_has_bounded_safe_diagnostic(self):
        self.finish()
        attempts = []
        incomplete = {'type': 'response.incomplete', 'response': {
            'incomplete_details': {'reason': 'max_output_tokens'},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'secret'}]}]}}

        def handler(request):
            attempts.append(request)
            return httpx.Response(200, text='data: ' + json.dumps(incomplete) + '\n\n',
                                  headers={'Content-Type': 'text/event-stream'})

        plan = self.new_plan(handler)
        with patch('facturador.auth.time.sleep'):
            with self.assertRaises(PlanError) as raised:
                plan.infer('Lee este ticket', [], Extraction)
        self.assertEqual(len(attempts), 2)
        error = raised.exception
        self.assertEqual((error.status, error.code, error.event_type),
                         ('transient', 'max_output_tokens', 'response.incomplete'))
        self.assertNotIn('secret', str(error))

    def test_quota_with_server_deadline_recovers_after_restart(self):
        self.finish()
        def limited(request):
            return httpx.Response(429, headers={'Retry-After': '120'},
                                  json={'error': {'code': 'subscription_sharing_usage_limit_exceeded'}})
        limited_plan = self.new_plan(limited)
        with self.assertRaises(PlanError):
            limited_plan.infer('Lectura', [], Extraction)
        self.assertEqual(limited_plan.status()['status'], 'limit')
        reopened = self.new_plan(lambda _: httpx.Response(200))
        deadline = read_json(reopened.session_path)['hold_until']
        with patch('time.time', return_value=deadline + 1):
            self.assertEqual(reopened.status()['status'], 'ready')
            self.assertTrue(reopened._credentials()[0])

    def test_incomplete_stream_never_returns_a_guess(self):
        self.finish()
        partial = self.new_plan(lambda _: httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"{}"}\n\n'))
        with self.assertRaisesRegex(PlanError, 'confirmarse'):
            partial.infer('Flujo incompleto', [], Extraction)

    def test_web_search_collects_streamed_answer_and_sources(self):
        self.finish()
        billing = 'https://facturas.example.org/emitir'
        evidence = 'https://cafeteria.example.org/facturacion'
        def searched(request):
            body = json.loads(request.content)
            self.assertEqual(body['tools'], [{'type':'web_search','search_context_size':'low'}])
            self.assertEqual(body['include'], ['web_search_call.action.sources'])
            events = [
                {'type':'response.output_item.done','item':{'type':'web_search_call',
                    'action':{'sources':[{'url':evidence}]}}},
                {'type':'response.output_item.done','item':{'type':'message','content':[
                    {'type':'output_text','text':json.dumps({'billing_url':billing,'evidence_url':evidence})}]}},
                {'type':'response.completed','response':{'output':[
                    {'type':'message','content':[{'type':'output_text','text':json.dumps(
                        {'billing_url':billing,'evidence_url':evidence})}]}]}},
            ]
            return httpx.Response(200, text=''.join('data: '+json.dumps(event)+'\n\n' for event in events),
                                  headers={'Content-Type':'text/event-stream'})
        plan = self.new_plan(searched)
        result, sources = plan.infer('Busca el portal', [{'type':'input_text','text':'Cafetería'}],
                                     PortalSearch, web_search=True)
        self.assertEqual((result.billing_url, result.evidence_url, sources), (billing,evidence,[evidence]))
