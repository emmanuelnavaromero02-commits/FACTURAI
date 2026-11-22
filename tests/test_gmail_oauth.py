import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from facturador import gmail
from facturador.app import create_app
from facturador.auth import read_json, write_private


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == '/profile':
            self.server.profile_reads += 1
            if self.server.gmail_available and self.headers.get('Authorization') == 'Bearer fake-access':
                self.reply(200, {'emailAddress': 'demo@example.invalid'})
            else:
                self.reply(403, {})
            return
        if path.path != '/authorize':
            return self.reply(404, {})
        request = dict((k, v[0]) for k, v in parse_qs(path.query).items())
        self.server.requests[request['state']] = request
        self.send_response(302)
        self.send_header('Location', request['redirect_uri'] + '?' + urlencode(
            {'code': 'code-' + request['state'], 'state': request['state']}))
        self.end_headers()

    def do_POST(self):
        if self.path != '/token':
            return self.reply(404, {})
        form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        if form.get('grant_type') == ['refresh_token'] and form.get('refresh_token') == ['fake-offline']:
            return self.reply(200, {'access_token': 'fake-renewed', 'expires_in': 3600})
        state = form['code'][0].removeprefix('code-')
        authorization = self.server.requests.pop(state, None)
        if not authorization:
            return self.reply(400, {})
        digest = hashlib.sha256(form['code_verifier'][0].encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
        valid = (authorization['code_challenge'] == challenge and authorization['code_challenge_method'] == 'S256'
                 and authorization['redirect_uri'] == form['redirect_uri'][0]
                 and authorization['client_id'] == form['client_id'][0]
                 and form['client_secret'] == ['fake-secret'])
        if not valid:
            return self.reply(400, {})
        self.reply(200, self.server.token_response)


class GmailOAuthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        self.provider.profile_reads = 0
        self.provider.requests = {}
        self.provider.gmail_available = True
        self.provider.token_response = {
            'access_token': 'fake-access', 'refresh_token': 'fake-offline', 'scope': gmail.SCOPE, 'expires_in': 3600}
        endpoint = 'http://127.0.0.1:' + str(self.provider.server_port)
        for name, path in [('AUTHORIZE', '/authorize'), ('TOKEN', '/token'), ('PROFILE', '/profile')]:
            self.enterContext(patch.object(gmail, name, endpoint + path))
        self.http = self.enterContext(httpx.Client(trust_env=False, follow_redirects=False))

    def panel(self, port=8765):
        root = self.root / str(port)
        self.origin = f'http://127.0.0.1:{port}'
        self.client = self.enterContext(TestClient(create_app(root, port=port), base_url=self.origin))
        self.headers = {'Origin': self.origin, 'X-App-Token': self.client.get('/api/state').json()['token']}
        self.upload_client()
        return root

    def upload_client(self, client='test', secret='fake-secret'):
        config = {'installed': {'client_id': client + '.apps.googleusercontent.com', 'client_secret': secret}}
        return self.client.post('/api/gmail/client', headers=self.headers,
                                files={'file': ('test-client.json', json.dumps(config), 'application/json')})

    def start(self):
        response = self.client.post('/api/gmail/connect', headers={**self.headers, 'X-Forwarded-Host': 'unused.invalid',
                                                                   'X-Forwarded-Port': '443'})
        self.assertEqual(response.status_code, 200)
        destination = response.json()['url']
        query = parse_qs(urlsplit(destination).query)
        self.assertEqual(query['redirect_uri'], [self.origin + '/auth/gmail/callback'])
        self.assertEqual(query['scope'], [gmail.SCOPE])
        self.assertEqual(query['access_type'], ['offline'])
        self.assertNotIn('code_verifier', query)
        self.assertNotIn('client_secret', query)
        result = self.http.get(destination)
        self.assertEqual(result.status_code, 302)
        return result.headers['Location']

    def callback(self, uri):
        response = self.client.get(uri, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return response.headers['Location']

    def test_return_to_actual_loopback_port_and_refresh_after_restart(self):
        for port in (8765, 18765):
            with self.subTest(port=port):
                root = self.panel(port)
                response = self.client.post('/api/gmail/connect', headers={**self.headers, 'Origin': 'https://unused.invalid'})
                self.assertEqual(response.status_code, 403)
                uri = self.start()
                self.assertIn('gmail-ok', self.callback(uri))
                self.assertIn('caducado', self.callback(uri))
                self.assertEqual(self.client.get('/api/state').json()['gmail'], 'connected')
                record = read_json(root/'gmail_token.json')
                record['expires_at'] = time.time() - 1
                write_private(root/'gmail_token.json', record)
                self.assertEqual(gmail.access_token(root), 'fake-renewed')
                self.assertEqual(read_json(root/'gmail_token.json')['refresh_token'], 'fake-offline')
        self.assertEqual(self.provider.profile_reads, 2)

    def test_retry_cancel_wrong_state_and_reconfigure_preserve_previous_account(self):
        root = self.panel()
        self.assertIn('gmail-ok', self.callback(self.start()))
        previous = (root/'gmail_token.json').read_bytes()
        self.assertEqual(self.upload_client().status_code, 200)
        self.assertEqual(self.upload_client(client='another').status_code, 422)
        self.assertEqual((root/'gmail_token.json').read_bytes(), previous)
        first, second = self.start(), self.start()
        self.assertIn('caducado', self.callback('/auth/gmail/callback?state=%C3%A9&code=invalid'))
        state = parse_qs(urlsplit(second).query)['state'][0]
        self.assertIn('consentimiento', self.callback('/auth/gmail/callback?' + urlencode(
            {'state': state, 'error': 'access_denied'})))
        self.assertIn('caducado', self.callback(second))
        self.assertIn('gmail-ok', self.callback(first))

    def test_expired_state_and_rejected_grant_cannot_create_a_connection(self):
        root = self.panel()
        uri = self.start()
        with patch.object(gmail.time, 'monotonic', return_value=time.monotonic() + 601):
            self.assertIn('caducado', self.callback(uri))
        self.assertEqual(self.provider.profile_reads, 0)
        uri = self.start()
        self.provider.requests.clear()
        self.assertIn('intercambio', self.callback(uri))
        self.assertFalse((root/'gmail_token.json').exists())

    def test_incomplete_grant_or_unavailable_mailbox_preserves_good_tokens(self):
        root = self.panel()
        self.assertIn('gmail-ok', self.callback(self.start()))
        previous = (root/'gmail_token.json').read_bytes()
        for changes, expected in [({'refresh_token': None}, 'sin-renovacion'), ({'scope': 'other'}, 'permiso')]:
            with patch.dict(self.provider.token_response, changes):
                self.assertIn(expected, self.callback(self.start()))
            self.assertEqual((root/'gmail_token.json').read_bytes(), previous)
        self.provider.gmail_available = False
        self.assertIn('buzon', self.callback(self.start()))
        self.assertEqual((root/'gmail_token.json').read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
