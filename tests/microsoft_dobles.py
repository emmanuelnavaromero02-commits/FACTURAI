"""In-process stand-in for Microsoft's sign-in (authorize and token endpoints, v2.0). No real network is used.

It keeps the rules the program depends on: the registered redirect address, PKCE, a code that works
once, renewal tokens that are replaced on every use and a permission the person can take away. The
access tokens it issues are shared with the IMAP and SMTP doubles, so a token only opens the
mailbox of the account it was issued for.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt

CLIENT = '11111111-2222-4333-8444-555555555555'
OTHER_CLIENT = '99999999-8888-4777-8666-555555555555'
PERSONAL_TENANT = '9188040d-6c67-4c5b-b112-36a304b66dad'
WORK_TENANT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
IMAP_SCOPE = 'https://outlook.office.com/IMAP.AccessAsUser.All'
SEND_SCOPE = 'https://outlook.office.com/SMTP.Send'
# Every secret this double hands out carries one of these marks, so a test can look for leaks.
MARKS = ('CODIGO-FICTICIO', 'ACCESO-FICTICIO', 'RENOVACION-FICTICIA', 'IDENTIDAD-FICTICIA')


def visit(address: str) -> tuple[int, str]:
    """What the person's browser does at Microsoft: one request, nothing followed and nothing logged."""
    parts = urlsplit(address)
    link = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        link.request('GET', parts.path + '?' + parts.query)
        reply = link.getresponse()
        reply.read()
        return reply.status, reply.getheader('Location') or ''
    finally:
        link.close()


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
        tenant, _, rest = address.path.strip('/').partition('/')
        if rest != 'oauth2/v2.0/authorize':
            return self.reply(404, {})
        query = {key: values[0] for key, values in parse_qs(address.query).items()}
        owner.authorizations.append({**query, 'tenant': tenant})
        registered = (query.get('client_id') == owner.client and tenant == owner.tenant
                      and owner.accepts(query.get('redirect_uri', '')))
        if (not registered or query.get('response_type') != 'code' or query.get('code_challenge_method') != 'S256'
                or not query.get('code_challenge') or not query.get('state')):
            # Microsoft shows its own error page and never returns to an address that is not registered.
            return self.reply(400, {'error': 'invalid_request'})
        if owner.declines:
            answer = {'error': 'access_denied', 'error_description': 'La persona canceló.', 'state': query['state']}
        else:
            owner.count += 1
            code = f'CODIGO-FICTICIO-{owner.count}'
            owner.codes[code] = {**query, 'account': owner.signed_in}
            answer = {'code': code, 'state': query['state']}
        mark = '?' if query.get('response_mode') == 'query' else '#'
        self.send_response(302)
        self.send_header('Location', query['redirect_uri'] + mark + urlencode(answer))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        owner = self.server.owner
        tenant, _, rest = urlsplit(self.path).path.strip('/').partition('/')
        if rest != 'oauth2/v2.0/token':
            return self.reply(404, {})
        form = {key: values[0] for key, values in
                parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()).items()}
        owner.token_requests.append({**form, 'tenant': tenant})
        if owner.unavailable:
            return self.reply(503, {'error': 'temporarily_unavailable'})
        if form.get('client_id') != owner.client or tenant != owner.tenant:
            return self.reply(400, {'error': 'unauthorized_client', 'error_codes': [700016]})
        if (owner.secret or 'client_secret' in form) and form.get('client_secret') != owner.secret:
            # A public client must not send a secret; a web one must send its own.
            return self.reply(401, {'error': 'invalid_client', 'error_codes': [700025 if not owner.secret else 7000215]})
        if form.get('grant_type') == 'authorization_code':
            grant = owner.codes.pop(form.get('code', ''), None)
            digest = hashlib.sha256(form.get('code_verifier', '').encode()).digest()
            proof = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
            if (not grant or grant['code_challenge'] != proof or grant['redirect_uri'] != form.get('redirect_uri')
                    or grant['client_id'] != form.get('client_id')):
                return self.reply(400, {'error': 'invalid_grant', 'error_codes': [54005]})
            scopes = [item for item in grant['scope'].split() if item in (IMAP_SCOPE, SEND_SCOPE)]
            return self.reply(200, owner.issue(grant['account'], scopes, offline='offline_access' in grant['scope'].split(),
                                               identity='openid' in grant['scope'].split()))
        if form.get('grant_type') == 'refresh_token':
            grant = owner.renewals.get(form.get('refresh_token', ''))
            if not grant or grant['account'] in owner.revoked:
                return self.reply(400, {'error': 'invalid_grant', 'error_description': 'AADSTS50173: revocado.',
                                        'error_codes': [50173]})
            if owner.single_use_renewals:
                del owner.renewals[form['refresh_token']]
            return self.reply(200, owner.issue(grant['account'], grant['scopes'], offline=True, identity=False))
        self.reply(400, {'error': 'unsupported_grant_type'})


class FakeMicrosoft:
    def __init__(self, client=CLIENT, tenant='common', secret=''):
        self.client, self.tenant, self.secret = client, tenant, secret
        self.redirects: set[str] = set()
        # The account that is signed in at Microsoft in the person's browser.
        self.signed_in = 'persona@outlook.example'
        self.accounts = {'persona@outlook.example': {'tid': PERSONAL_TENANT}}
        self.access: dict[str, str] = {}     # access token -> mailbox address; the mail doubles read it
        self.renewals: dict[str, dict] = {}  # renewal token -> grant
        self.codes: dict[str, dict] = {}
        self.revoked: set[str] = set()
        self.authorizations, self.token_requests = [], []
        self.count = 0
        self.declines = False
        self.unavailable = False
        self.lifetime = 3600
        self.rotates = True               # A new renewal token with every use, as Microsoft does.
        self.single_use_renewals = True   # Stricter than Microsoft: it shows that the new one was kept.
        self.grants_renewal = True
        self.names = None                 # Claims of the identity token, when they should differ from the account.
        self.granted = None               # Permissions granted, when they should differ from the ones asked.
        self.upper_scopes = False
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.owner = self
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def authorize(self) -> str:
        return self.url + '/{tenant}/oauth2/v2.0/authorize'

    @property
    def token(self) -> str:
        return self.url + '/{tenant}/oauth2/v2.0/token'

    def accepts(self, redirect: str) -> bool:
        """Exact match, except that the port of a «localhost» address is ignored."""
        def shape(value):
            parts = urlsplit(value)
            port = None if parts.hostname == 'localhost' else parts.port
            return parts.scheme, parts.hostname, port, parts.path, parts.query, parts.fragment
        return any(shape(redirect) == shape(item) for item in self.redirects)

    def issue(self, account: str, scopes: list[str], *, offline: bool, identity: bool) -> dict:
        self.count += 1
        scopes = list(self.granted) if self.granted is not None else list(scopes)
        access = f'ACCESO-FICTICIO-{self.count}'
        self.access[access] = account
        text = ' '.join(scopes)
        answer = {'token_type': 'Bearer', 'access_token': access, 'expires_in': self.lifetime,
                  'scope': text.upper() if self.upper_scopes else text}
        if offline and self.grants_renewal:
            if self.rotates or not any(grant['account'] == account for grant in self.renewals.values()):
                renewal = f'RENOVACION-FICTICIA-{self.count}'
                self.renewals[renewal] = {'account': account, 'scopes': scopes}
                answer['refresh_token'] = renewal
        if identity:
            claims = self.names if self.names is not None else {
                'email': account, 'preferred_username': account, **self.accounts.get(account, {})}
            answer['id_token'] = jwt.encode({'aud': self.client, 'sub': 'IDENTIDAD-FICTICIA', **claims},
                                            'llave-de-prueba-de-treinta-y-dos-bytes', algorithm='HS256')
        return answer

    def revoke(self, account: str) -> None:
        """The person takes the permission away at Microsoft: nothing issued for the account works any more."""
        self.revoked.add(account)
        for token in [token for token, owner in self.access.items() if owner == account]:
            del self.access[token]

    def expire_access(self, account: str) -> None:
        """Every access token of the account stops working at the mail servers; renewal still does."""
        for token in [token for token, owner in self.access.items() if owner == account]:
            del self.access[token]
