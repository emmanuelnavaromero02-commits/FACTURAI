from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from .read_http import retry_seconds

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
AUTHORIZATION = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
JWKS = ISSUER + '/.well-known/jwks.json'
PUBLIC_CLIENT = 'dynamic_agent_client'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
PLAN_SCOPES = {'resource.invoke', 'chatgpt.tokens.use.direct'}
DEFAULT_MODEL = 'gpt-6.2-sol'
CALLBACK = '/auth/chatgpt/callback'


class PlanError(ValueError):
    def __init__(self, message: str, status: str = 'unavailable', retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def read_json(path: Path, fallback=None):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding='utf-8'))


def write_private(path: Path, value: dict) -> None:
    """Atomic replacement of a credential or profile owned by this installation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def decode_event_stream(lines):
    """Responses uses server-sent events. Consume the authoritative final event."""
    data = []
    for line in lines:
        if line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
        elif not line and data:
            payload = '\n'.join(data)
            data.clear()
            if payload != '[DONE]':
                yield json.loads(payload)
    if data:
        payload = '\n'.join(data)
        if payload != '[DONE]':
            yield json.loads(payload)


class ChatGPTPlan:
    def __init__(self, root: Path, port: int = 8765, client: httpx.Client | None = None):
        self.root = root / 'auth'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_path = self.root / 'chatgpt.json'
        self.redirect = f'http://127.0.0.1:{port}{CALLBACK}'
        self.lock = threading.RLock()
        self.pending: dict | None = None
        self.client = client or httpx.Client(timeout=httpx.Timeout(90, connect=12), follow_redirects=False)

    @staticmethod
    def _active_hold(record):
        hold = record.get('hold', '')
        until = record.get('hold_until', 0)
        return '' if hold == 'limit' and until and until <= time.time() else hold

    def status(self) -> dict:
        record = read_json(self.session_path, {})
        if hold := self._active_hold(record):
            state = hold
        elif not record.get('access_token'):
            state = 'disconnected'
        elif not PLAN_SCOPES.issubset(record.get('scopes', [])):
            state = 'identity_only'
        else:
            state = 'ready'
        return {'status': state, 'email': record.get('email', ''), 'model': record.get('model', DEFAULT_MODEL),
                'message': record.get('message', '') if state != 'ready' else '', 'registered': bool(record.get('client_id'))}

    def start(self, new_account: bool = False, consent: bool = False) -> str:
        with self.lock:
            record = read_json(self.session_path, {})
            client_id = (record.get('client_id') if not new_account else None) or PUBLIC_CLIENT
            host = read_json(self.root / 'host.json', {})
            if not host:
                host = {'id': uuid.uuid4().urn}
                write_private(self.root / 'host.json', host)
            state = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            self.pending = {'state': state, 'nonce': nonce, 'verifier': verifier, 'client_id': client_id,
                            'subject': record.get('subject') if client_id != PUBLIC_CLIENT else None,
                            'deadline': time.monotonic() + 600}
            params = {'client_id': client_id, 'ext_agent_host_id': host['id'], 'response_type': 'code',
                      'redirect_uri': self.redirect, 'scope': SCOPES, 'resource': RESOURCE, 'state': state,
                      'nonce': nonce, 'code_challenge_method': 'S256', 'code_challenge': challenge}
            if client_id == PUBLIC_CLIENT:
                params['agent_name_hint'] = 'Facturador'
            elif consent:
                params['prompt'] = 'consent'
            if client_id != PUBLIC_CLIENT and record.get('email'):
                params['login_hint'] = record['email']
            return AUTHORIZATION + '?' + urlencode(params)

    def finish(self, state: str, code: str, issued_client: str, error: str = '') -> None:
        with self.lock:
            attempt = self.pending
            if not attempt or not secrets.compare_digest(attempt['state'], state) or attempt['deadline'] < time.monotonic():
                raise PlanError('La conexión caducó. Inicia un nuevo intento desde el panel.')
            self.pending = None
            if error or not code:
                raise PlanError('La conexión con ChatGPT no se completó.')
            client_id = attempt['client_id']
            if client_id == PUBLIC_CLIENT:
                if not re.fullmatch(r'oaiapp_[A-Za-z0-9_-]{1,180}', issued_client):
                    raise PlanError('OpenAI no devolvió el registro de esta aplicación.')
                client_id = issued_client
            elif issued_client and not secrets.compare_digest(issued_client, client_id):
                raise PlanError('La conexión recibida corresponde a otro registro.')
            tokens = self._exchange({'grant_type': 'authorization_code', 'client_id': client_id,
                                     'code': code, 'code_verifier': attempt['verifier'],
                                     'redirect_uri': self.redirect, 'resource': RESOURCE})
            identity = self._identity(tokens.get('id_token', ''), client_id, attempt['nonce'])
            if attempt['subject'] and identity['sub'] != attempt['subject']:
                raise PlanError('Selecciona la cuenta que ya vinculaste o usa Otra cuenta.')
            previous = read_json(self.session_path, {})
            record = {'client_id': client_id, 'subject': identity['sub'], 'email': identity.get('email', ''),
                      'model': previous.get('model', DEFAULT_MODEL) if client_id == previous.get('client_id') else DEFAULT_MODEL}
            write_private(self.session_path, self._with_tokens(record, tokens))

    def _identity(self, token: str, client_id: str, nonce: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
            response = self.client.get(JWKS)
            response.raise_for_status()
            jwk = next(x for x in response.json()['keys'] if x.get('kid') == header.get('kid'))
            key = jwt.PyJWK.from_dict(jwk, algorithm='RS256').key
            claims = jwt.decode(token, key, algorithms=['RS256'], issuer=ISSUER, audience=client_id,
                                options={'require': ['iss', 'aud', 'exp', 'iat', 'sub', 'nonce']})
        except (KeyError, StopIteration, TypeError, ValueError, jwt.PyJWTError, httpx.HTTPError) as exc:
            raise PlanError('No se pudo comprobar la identidad devuelta por OpenAI.') from exc
        if claims['nonce'] != nonce or not claims['sub']:
            raise PlanError('La identidad no coincide con este intento de conexión.')
        if isinstance(claims['aud'], list) and len(claims['aud']) > 1 and claims.get('azp') != client_id:
            raise PlanError('La identidad no está dirigida a esta aplicación.')
        return claims

    def _exchange(self, form: dict) -> dict:
        try:
            response = self.client.post(TOKEN, data=form)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PlanError('OpenAI no respondió al conectar el plan. Intenta de nuevo.') from exc
        if not isinstance(data, dict) or not response.is_success:
            error = data.get('error') if isinstance(data, dict) else None
            if isinstance(error, dict):
                error = error.get('code')
            if error in {'invalid_grant', 'invalid_refresh_token', 'token_expired', 'refresh_token_expired',
                         'refresh_token_invalidated', 'refresh_token_reused'}:
                raise PlanError('La sesión expiró o se revocó. Vuelve a conectar ChatGPT.', 'reconnect')
            raise PlanError(f'OpenAI rechazó la conexión (HTTP {response.status_code}).')
        return data

    @staticmethod
    def _with_tokens(record: dict, tokens: dict) -> dict:
        access = tokens.get('access_token')
        scopes = tokens.get('scope', ' '.join(record.get('scopes', [])))
        if not isinstance(access, str) or not access or str(tokens.get('token_type', 'Bearer')).lower() != 'bearer':
            raise PlanError('OpenAI no devolvió una sesión utilizable.')
        try:
            lifetime = max(0, int(tokens.get('expires_in', 3600)))
        except (TypeError, ValueError) as exc:
            raise PlanError('La sesión tiene una vigencia inválida.') from exc
        if not isinstance(scopes, str):
            raise PlanError('OpenAI no devolvió permisos utilizables.')
        return {**record, 'access_token': access, 'refresh_token': tokens.get('refresh_token', ''),
                'scopes': scopes.split(), 'expires_at': time.time() + lifetime, 'hold': '', 'message': ''}

    def _credentials(self) -> tuple[str, str]:
        with self.lock:
            record = read_json(self.session_path, {})
            if hold := self._active_hold(record):
                raise PlanError(record.get('message') or 'Revisa tu conexión con ChatGPT.', hold)
            if not record.get('access_token') or not PLAN_SCOPES.issubset(record.get('scopes', [])):
                raise PlanError('Conecta ChatGPT y autoriza el uso de tu plan desde Ajustes.', 'disconnected')
            if record['expires_at'] - time.time() < 60 and record.get('refresh_token'):
                try:
                    tokens = self._exchange({'grant_type': 'refresh_token', 'client_id': record['client_id'],
                                             'refresh_token': record['refresh_token'], 'resource': RESOURCE})
                except PlanError as exc:
                    if exc.status == 'reconnect':
                        self._clear_tokens(record, str(exc))
                        raise
                    if record['expires_at'] <= time.time():
                        raise
                else:
                    record = self._with_tokens(record, tokens)
                    write_private(self.session_path, record)
            if record['expires_at'] <= time.time():
                raise PlanError('La sesión expiró. Vuelve a conectar ChatGPT.', 'reconnect')
            if not PLAN_SCOPES.issubset(record.get('scopes', [])):
                raise PlanError('El plan requiere autorización otra vez.', 'identity_only')
            return record['access_token'], record.get('model', DEFAULT_MODEL)

    def _clear_tokens(self, record: dict, message: str = '') -> None:
        keep = {key: record[key] for key in ('client_id', 'subject', 'email', 'model') if key in record}
        if message:
            keep.update(hold='reconnect', message=message)
        write_private(self.session_path, keep)

    def disconnect(self) -> bool:
        with self.lock:
            record = read_json(self.session_path, {})
            refresh = record.get('refresh_token')
            revoked = not refresh
            try:
                if refresh:
                    metadata = self.client.get(ISSUER + '/.well-known/openid-configuration')
                    metadata.raise_for_status()
                    endpoint = metadata.json().get('revocation_endpoint', '')
                    url = urlsplit(endpoint)
                    if url.scheme == 'https' and url.netloc == 'auth.openai.com':
                        revoked = self.client.post(endpoint, data={'token': refresh, 'token_type_hint': 'refresh_token',
                                                                   'client_id': record['client_id']}).is_success
            except (ValueError, httpx.HTTPError):
                pass
            self.pending = None
            self._clear_tokens(record)
            return revoked

    def clear_limit(self) -> None:
        with self.lock:
            record = read_json(self.session_path, {})
            if record.get('hold') == 'limit':
                record.update(hold='', message='')
                write_private(self.session_path, record)

    def _error(self, raw: dict, status: int = 0, access: str = '', retry_after: int = 0) -> PlanError:
        error = raw.get('error') or raw
        if not isinstance(error, dict):
            error = {}
        code = str(error.get('code', ''))
        if code == 'subscription_sharing_usage_limit_exceeded' or (status == 429 and code == 'insufficient_quota'):
            message = (f'El cupo de ChatGPT está en pausa. La lectura se reanudará en {retry_after} segundos.'
                       if retry_after else 'El cupo de ChatGPT está en pausa. Consulta el uso de tu plan y retoma cuando esté disponible.')
            with self.lock:
                record = read_json(self.session_path, {})
                if access and record.get('access_token') == access:
                    record.update(hold='limit', message=message, hold_until=time.time() + retry_after if retry_after else 0)
                    write_private(self.session_path, record)
            return PlanError(message, 'limit', retry_after)
        if status in {408, 425, 429, 500, 502, 503, 504} or code in {'server_error', 'rate_limit_exceeded'}:
            return PlanError('ChatGPT está temporalmente ocupado o sin respuesta.', 'transient', retry_after)
        if code == 'subscription_sharing_user_not_eligible':
            return PlanError('El plan o espacio elegido no admite el uso en esta aplicación.', 'ineligible')
        if code == 'subscription_sharing_unsupported_capability' or code == 'model_not_found':
            field = str(error.get('param', ''))
            suffix = f' ({field})' if re.fullmatch(r'[a-zA-Z0-9_.\[\]-]{1,80}', field) else ''
            return PlanError('El modelo elegido no admite esta solicitud con el plan' + suffix + '.', 'model')
        if status == 401 or code in {'subscription_sharing_invalid_user', 'chatpass_v2_invalid_authorization_context'}:
            message = 'OpenAI no aceptó la sesión. Vuelve a conectar ChatGPT.'
            with self.lock:
                record = read_json(self.session_path, {})
                if access and record.get('access_token') == access:
                    record.update(hold='reconnect', message=message)
                    write_private(self.session_path, record)
            return PlanError(message, 'reconnect')
        if status == 403:
            return PlanError('OpenAI no autorizó usar el plan para esta solicitud.', 'ineligible')
        suffix = f' (HTTP {status})' if status else ''
        return PlanError('ChatGPT no pudo completar la solicitud' + suffix + '.')

    @staticmethod
    def _body(response: httpx.Response) -> dict:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def models(self) -> list[dict]:
        access, _ = self._credentials()
        try:
            response = self.client.get(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + access})
            if not response.is_success:
                raise self._error(self._body(response), response.status_code, access)
            data = self._body(response)
            return [{'id': model['slug'], 'name': model.get('display_name', model['slug'])}
                    for model in data.get('models', []) if model.get('visibility') == 'list' and model.get('slug')]
        except httpx.HTTPError as exc:
            raise PlanError('No se pudo consultar los modelos de tu plan.') from exc

    def select_model(self, model: str) -> None:
        with self.lock:
            if model not in {entry['id'] for entry in self.models()}:
                raise PlanError('El modelo no aparece en la lista de este plan.', 'model')
            record = read_json(self.session_path, {})
            record['model'] = model
            write_private(self.session_path, record)

    def infer(self, instructions, content, output_type, *, effort='medium', web_search=False):
        access, model = self._credentials()
        request = {'model': model, 'reasoning': {'effort': effort}, 'store': False, 'stream': True,
                   'instructions': instructions, 'input': [{'role': 'user', 'content': content}],
                   'text': {'format': {'type': 'json_schema', 'name': output_type.__name__.lower(),
                                       'strict': True, 'schema': output_type.model_json_schema()}}}
        if web_search:
            request.update(tools=[{'type':'web_search', 'search_context_size':'low'}],
                           include=['web_search_call.action.sources'])
        try:
            with self.client.stream('POST', RESOURCE + '/responses', json=request,
                                    headers={'Authorization': 'Bearer ' + access}) as response:
                if not response.is_success:
                    response.read()
                    raise self._error(self._body(response), response.status_code, access,
                                      retry_seconds(response.headers.get('retry-after')))
                streamed_items = []
                for event in decode_event_stream(response.iter_lines()):
                    kind = event.get('type')
                    if kind in {'error', 'response.failed', 'response.incomplete'}:
                        raise self._error(event.get('response') or event, access=access)
                    if web_search and kind == 'response.output_item.done':
                        streamed_items.append(event['item'])
                    if kind == 'response.completed':
                        items = event['response'].get('output') or streamed_items
                        parts = [part for item in items
                                 if item.get('type') == 'message' for part in item.get('content', [])]
                        if any(p.get('type') == 'refusal' for p in parts):
                            raise PlanError('ChatGPT pidió que revises este ticket manualmente.')
                        answer = ''.join(part['text'] for part in parts if part.get('type') == 'output_text')
                        if not answer or len(answer) > 30_000:
                            raise PlanError('ChatGPT no devolvió una respuesta utilizable.')
                        parsed = output_type.model_validate_json(answer)
                        if web_search:
                            sources = [source.get('url') for item in [*items, *streamed_items]
                                       if item.get('type') == 'web_search_call'
                                       for source in item.get('action', {}).get('sources', [])]
                            return parsed, list(dict.fromkeys(url for url in sources if isinstance(url, str)))
                        return parsed
        except httpx.HTTPError as exc:
            raise PlanError('Se interrumpió la conexión con ChatGPT.', 'transient') from exc
        except (TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, PlanError):
                raise
            raise PlanError('ChatGPT devolvió datos incompletos. Revisa el ticket en el panel.') from exc
        raise PlanError('La respuesta de ChatGPT terminó antes de confirmarse.', 'transient')

    def close(self) -> None:
        self.client.close()
