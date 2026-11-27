"""In-process stand-in for Google's sign-in (authorization and token endpoints) and Gmail's profile. No real network.

It keeps the rules the program depends on: a client known by its identifier and its secret, the exact
return address for a «web» client and any port of the same computer for a desktop one, PKCE, a code
that works once and only for the address it was asked for, and a permission the person can take
away. The answer goes back in the address, with the extra values Google adds to it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from microsoft_dobles import visit  # One request as the person's browser makes it: nothing followed, nothing logged.

__all__ = ['ADDRESS', 'DESKTOP_CLIENT', 'MARKS', 'OTHER_CLIENT', 'SCOPE', 'SEND_SCOPE', 'WEB_CLIENT', 'FakeGoogle', 'visit']

WEB_CLIENT = '111111111111-webficticio.apps.googleusercontent.com'
DESKTOP_CLIENT = '222222222222-escritorioficticio.apps.googleusercontent.com'
OTHER_CLIENT = '333333333333-otroficticio.apps.googleusercontent.com'
SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'
ADDRESS = 'persona@gmail.example'
SECRET_MARK = 'SECRETO-FICTICIO'
# Every secret this double hands out carries one of these marks, so a test can look for leaks.
MARKS = (SECRET_MARK, 'CODIGO-FICTICIO', 'ACCESO-FICTICIO', 'RENOVACION-FICTICIA')
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        owner = self.server.owner
        address = urlsplit(self.path)
        if address.path == '/gmail/v1/users/me/profile':
            owner.profile_reads += 1
            account = owner.access.get(self.headers.get('Authorization', '').removeprefix('Bearer '))
            if not account or not owner.mailbox_available:
                return self.reply(403 if account else 401, {})
            return self.reply(200, {'emailAddress': account})
        if address.path != '/o/oauth2/v2/auth':
            return self.reply(404, {})
        query = {key: values[0] for key, values in parse_qs(address.query).items()}
        owner.authorizations.append(query)
        if (not owner.accepts(query.get('client_id', ''), query.get('redirect_uri', '')) or query.get('response_type') != 'code'
                or SCOPE not in query.get('scope', '').split() or not query.get('state')
                or (query.get('code_challenge') and query.get('code_challenge_method') != 'S256')):
            # Google shows its own error page and never returns to an address that is not registered.
            return self.reply(400, {'error': 'invalid_request'})
        if owner.declines:
            answer = {'error': 'access_denied', 'state': query['state']}
        else:
            owner.count += 1
            # Real codes carry a slash, which the address has to keep intact.
            code = f'4/0CODIGO-FICTICIO-{owner.count}'
            owner.codes[code] = {**query, 'account': owner.signed_in}
            # Google adds values of its own to the answer; the program has no use for them.
            answer = {'state': query['state'], 'code': code, 'scope': query['scope'], 'authuser': '0',
                      'prompt': 'consent', 'iss': 'https://accounts.google.com'}
        mark = '#' if query.get('response_mode') == 'fragment' else '?'
        self.send_response(302)
        self.send_header('Location', query['redirect_uri'] + mark + urlencode(answer))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        owner = self.server.owner
        if urlsplit(self.path).path != '/token':
            return self.reply(404, {})
        form = {key: values[0] for key, values in
                parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()).items()}
        owner.token_requests.append(form)
        if owner.unavailable:
            return self.reply(503, {'error': 'temporarily_unavailable'})
        client = owner.clients.get(form.get('client_id', ''))
        if not client or form.get('client_secret') != client['secret']:
            return self.reply(401, {'error': 'invalid_client'})
        if form.get('grant_type') == 'authorization_code':
            grant = owner.codes.pop(form.get('code', ''), None)
            digest = hashlib.sha256(form.get('code_verifier', '').encode()).digest()
            proof = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
            if (not grant or grant['client_id'] != form['client_id'] or grant['redirect_uri'] != form.get('redirect_uri')
                    or (grant.get('code_challenge') and grant['code_challenge'] != proof)):
                return self.reply(400, {'error': 'invalid_grant'})
            scopes = [item for item in grant['scope'].split() if item in (SCOPE, SEND_SCOPE)]
            return self.reply(200, owner.issue(form['client_id'], grant['account'], scopes,
                                               offline=grant.get('access_type') == 'offline'))
        if form.get('grant_type') == 'refresh_token':
            grant = owner.renewals.get(form.get('refresh_token', ''))
            # A permission is renewed only for the client that received it.
            if not grant or grant['client_id'] != form['client_id'] or grant['account'] in owner.revoked:
                return self.reply(400, {'error': 'invalid_grant'})
            return self.reply(200, owner.issue(form['client_id'], grant['account'], grant['scopes'], offline=False))
        self.reply(400, {'error': 'unsupported_grant_type'})


class FakeGoogle:
    def __init__(self):
        self.clients: dict[str, dict] = {}
        # The account that is signed in at Google in the person's browser.
        self.signed_in = ADDRESS
        self.access: dict[str, str] = {}     # access token -> mailbox address
        self.renewals: dict[str, dict] = {}  # renewal token -> grant
        self.codes: dict[str, dict] = {}
        self.revoked: set[str] = set()
        self.authorizations, self.token_requests = [], []
        self.count = 0
        self.profile_reads = 0
        self.declines = False
        self.unavailable = False
        self.mailbox_available = True
        self.grants_renewal = True
        self.granted = None                  # Permissions granted, when they should differ from the ones asked.
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.owner = self
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def authorize(self) -> str:
        return self.url + '/o/oauth2/v2/auth'

    @property
    def token(self) -> str:
        return self.url + '/token'

    @property
    def profile(self) -> str:
        return self.url + '/gmail/v1/users/me/profile'

    def register(self, client_id: str, kind: str, *redirects: str) -> bytes:
        """Create a client and return the JSON that Google Cloud lets its owner download."""
        secret = f'{SECRET_MARK}-{len(self.clients) + 1}'
        self.clients[client_id] = {'secret': secret, 'kind': kind, 'redirects': set(redirects)}
        return json.dumps({kind: {
            'client_id': client_id, 'project_id': 'proyecto-ficticio', 'client_secret': secret,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': list(redirects) or ['http://localhost']}}).encode()

    def accepts(self, client_id: str, redirect: str) -> bool:
        """A «web» client: the exact registered address. A desktop one: this same computer, on any port."""
        client = self.clients.get(client_id)
        if not client:
            return False
        if client['kind'] == 'web':
            return redirect in client['redirects']
        parts = urlsplit(redirect)
        return parts.scheme == 'http' and parts.hostname in LOOPBACK and not parts.query and not parts.fragment

    def issue(self, client_id: str, account: str, scopes: list[str], *, offline: bool) -> dict:
        self.count += 1
        scopes = list(self.granted) if self.granted is not None else list(scopes)
        access = f'ya29.ACCESO-FICTICIO-{self.count}'
        self.access[access] = account
        answer = {'token_type': 'Bearer', 'access_token': access, 'expires_in': 3599, 'scope': ' '.join(scopes)}
        if offline and self.grants_renewal:
            renewal = f'1//RENOVACION-FICTICIA-{self.count}'
            self.renewals[renewal] = {'client_id': client_id, 'account': account, 'scopes': scopes}
            answer['refresh_token'] = renewal
        return answer

    def revoke(self, account: str) -> None:
        """The person takes the permission away at Google: nothing issued for the account works any more."""
        self.revoked.add(account)
        for token in [token for token, owner in self.access.items() if owner == account]:
            del self.access[token]
