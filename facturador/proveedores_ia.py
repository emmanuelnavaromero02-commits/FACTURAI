"""Proveedores de IA con la misma interfaz que `ChatGPTPlan` y un selector que delega en el activo.

- `OpenAIKeyProvider`: llave de API de OpenAI, misma petición `/v1/responses` que usa el plan.
- `AnthropicKeyProvider`: llave de API de Anthropic (Claude) por `/v1/messages`, salida JSON con esquema.
- `SelectorIA`: lo que el resto del programa recibe como «plan». Sin configurar nada delega en el
  plan de ChatGPT exactamente como hoy; además deja una fila de uso por llamada (sin contenido).

Los errores de cada proveedor se traducen a `PlanError` con los estados que el programa ya entiende:
`transient` (reintentar), `limit` (cupo o saldo, con espera opcional), `model`, `reconnect`,
`ineligible`, `disconnected` y `unavailable`. Ningún mensaje lleva la llave.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

import httpx

from .auth import DEFAULT_MODEL, FALLBACK_EFFORT, ChatGPTPlan, PlanError, decode_event_stream, strict_schema
from .ia_ajustes import CHATGPT, KEY_PROVIDERS, TIERS, AjustesIA, tier_for
from .read_http import retry_seconds
from .uso_ia import RegistroUso, tarea_actual

OPENAI_API = 'https://api.openai.com/v1'
ANTHROPIC_API = 'https://api.anthropic.com/v1'
ANTHROPIC_VERSION = '2026-10-21'
ANTHROPIC_DEFAULT_MODEL = 'claude-opus-5-5'
ANTHROPIC_MAX_TOKENS = 16000
# Server-side refusal fallback, routed by refusal category (no model list to maintain).
ANTHROPIC_FALLBACK_BETA = 'server-side-fallback-2026-07-01'
ANTHROPIC_FALLBACK_MODELS = {'claude-fable-5-2', 'claude-fable-5-1', 'claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5-5'}
# Web search with dynamic filtering exists on these families; older models keep the basic tool.
ANTHROPIC_DYNAMIC_SEARCH = re.compile(r'claude-(?:fable|mythos)-|claude-opus-(?:5|4-8|4-7|4-6)|claude-sonnet-(?:5|4-6)')
ANTHROPIC_EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}
ANTHROPIC_IMAGES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
MAX_CONTINUATIONS = 3
MAX_ANSWER = 30_000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
# Keywords that structured outputs on Claude do not accept; Pydantic still enforces them afterwards.
UNSUPPORTED_SCHEMA = {'minLength', 'maxLength', 'pattern', 'minimum', 'maximum', 'exclusiveMinimum',
                      'exclusiveMaximum', 'multipleOf', 'minItems', 'maxItems', 'uniqueItems', 'default',
                      'minProperties', 'maxProperties'}
SUPPORTED_FORMATS = {'date-time', 'time', 'date', 'duration', 'email', 'hostname', 'uri', 'ipv4', 'ipv6', 'uuid'}


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def body_of(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def anthropic_schema(output_type) -> dict:
    """Strict schema (every property required, no extras) without keywords Claude rejects."""
    schema = strict_schema(output_type)

    def visit(node):
        if not isinstance(node, dict):
            return
        for keyword in UNSUPPORTED_SCHEMA & set(node):
            node.pop(keyword)
        if node.get('format') and node['format'] not in SUPPORTED_FORMATS:
            node.pop('format')
        for keyword in ('properties', '$defs', 'definitions'):
            for child in (node.get(keyword) or {}).values():
                visit(child)
        for keyword in ('items', 'not', 'additionalProperties'):
            visit(node.get(keyword))
        for keyword in ('anyOf', 'allOf', 'oneOf', 'prefixItems'):
            for child in node.get(keyword) or []:
                visit(child)
    visit(schema)
    return schema


def anthropic_content(content: list[dict]) -> list[dict]:
    """Responses-style parts (input_text, input_image with a data URL) → Messages content blocks."""
    result = []
    for part in content:
        kind = part.get('type') if isinstance(part, dict) else None
        if kind == 'input_text':
            text = str(part.get('text') or '')
            if text.strip():
                result.append({'type': 'text', 'text': text})
        elif kind == 'input_image':
            match = re.fullmatch(r'data:([\w/+.-]+);base64,([A-Za-z0-9+/=\s]+)', str(part.get('image_url') or ''))
            if not match or match.group(1) not in ANTHROPIC_IMAGES:
                raise PlanError('Esta imagen tiene un formato que Claude no recibe. Usa JPEG o PNG.', 'model')
            result.append({'type': 'image', 'source': {'type': 'base64', 'media_type': match.group(1),
                                                       'data': re.sub(r'\s', '', match.group(2))}})
        else:
            raise PlanError('La solicitud tiene un tipo de contenido que el proveedor no recibe.', 'model')
    if not result:
        raise PlanError('La solicitud no tiene contenido para el modelo.', 'model')
    return result


class KeyProvider:
    """Shared behaviour of the pay-per-use providers: key, holds, tier models and retries."""
    name = ''
    label = ''
    default_model = ''
    fallback_effort: str | None = None
    timeout = httpx.Timeout(300, connect=12)

    def __init__(self, settings: AjustesIA, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=self.timeout, follow_redirects=False)
        self.lock = threading.RLock()
        self.hold: dict = {}  # In memory: {'status', 'message', 'until', 'key'} (key fingerprint only)
        self._rejected_efforts: set[tuple[str, str]] = set()
        self._models_cache: tuple[float, str, list] = (0.0, '', [])

    # -- configuration
    def _key(self) -> str:
        key = self.settings.key(self.name)
        if not key:
            raise PlanError(f'Escribe la llave de {self.label} en Ajustes.', 'disconnected')
        return key

    def tier_models(self) -> dict:
        saved = self.settings.models(self.name)
        normal = saved.get('normal') or self.default_model
        return {'rapido': saved.get('rapido') or normal, 'normal': normal, 'dificil': saved.get('dificil') or normal}

    def model_for(self, effort: str) -> str:
        return self.tier_models()[tier_for(effort)]

    # -- holds (limit / reconnect), same meaning as the plan's
    def _active_hold(self, key: str = '') -> dict:
        with self.lock:
            hold = self.hold
            if not hold:
                return {}
            if key and hold.get('key') != fingerprint(key):
                self.hold = {}  # A new key replaces the one that was rejected.
                return {}
            if hold['status'] == 'limit' and hold.get('until') and hold['until'] <= time.time():
                self.hold = {}
                return {}
            return dict(hold)

    def _set_hold(self, key: str, status: str, message: str, retry_after: int = 0) -> None:
        with self.lock:
            self.hold = {'status': status, 'message': message, 'key': fingerprint(key),
                         'until': time.time() + retry_after if retry_after else 0}

    def status(self) -> dict:
        key = self.settings.key(self.name)
        model = self.tier_models()['normal']
        if not key:
            return {'status': 'disconnected', 'email': '', 'model': model,
                    'message': f'Escribe la llave de {self.label} en Ajustes.', 'registered': False}
        if hold := self._active_hold(key):
            return {'status': hold['status'], 'email': '', 'model': model, 'message': hold['message'], 'registered': True}
        return {'status': 'ready', 'email': '', 'model': model, 'message': '', 'registered': True}

    def clear_limit(self) -> None:
        with self.lock:
            self.hold = {}

    # OAuth steps belong to the ChatGPT plan only.
    def start(self, new_account: bool = False, consent: bool = False) -> str:
        raise PlanError('Este proveedor usa una llave de API; escríbela en Ajustes.', 'unavailable')

    def finish(self, state: str, code: str, issued_client: str, error: str = '') -> None:
        raise PlanError('Este proveedor usa una llave de API; escríbela en Ajustes.', 'unavailable')

    def disconnect(self) -> bool:
        return False

    # -- models
    def models(self) -> list[dict]:
        key = self._key()
        with self.lock:
            at, owner, cached = self._models_cache
            if owner == fingerprint(key) and time.monotonic() - at < 600:
                return list(cached)
        listed = self._list_models(key)
        with self.lock:
            self._models_cache = (time.monotonic(), fingerprint(key), listed)
        return list(listed)

    def select_model(self, model: str, tier: str = 'normal') -> None:
        if tier not in TIERS:
            raise PlanError('Los niveles son rápido, normal y difícil.', 'model')
        if model not in {entry['id'] for entry in self.models()}:
            raise PlanError('El modelo no aparece en la lista de este proveedor.', 'model')
        self.settings.set_models(self.name, {tier: model})

    def check_key(self, key: str) -> None:
        """Minimal proof that a key works: list its models. Raises PlanError; never stores anything."""
        self._list_models(key, probe=True)

    # -- inference
    def infer(self, instructions, content, output_type, *, effort='medium', web_search=False):
        return self.infer_measured(instructions, content, output_type, effort=effort, web_search=web_search)[0]

    def infer_measured(self, instructions, content, output_type, *, effort='medium', web_search=False):
        """Like `infer`, plus usage: {'model', 'input_tokens', 'output_tokens'} (None when not reported)."""
        key = self._key()
        if hold := self._active_hold(key):
            remaining = int(max(0, hold.get('until', 0) - time.time())) if hold.get('until') else 0
            raise PlanError(hold['message'], hold['status'], remaining)
        model = self.model_for(effort)
        asked = self._effort(effort)
        with self.lock:
            if (model, asked) in self._rejected_efforts:
                asked = self.fallback_effort
        retried = False
        while True:
            try:
                return self._infer_once(key, model, instructions, content, output_type, asked, web_search)
            except PlanError as exc:
                if getattr(exc, 'effort_rejected', False) and asked != self.fallback_effort:
                    # The model refused this level: ask once at the fallback and never again at this one.
                    with self.lock:
                        self._rejected_efforts.add((model, asked))
                    asked = self.fallback_effort
                    continue
                if retried or exc.status != 'transient' or exc.retry_after > 2:
                    raise
                retried = True
                time.sleep(0.5)

    def _effort(self, effort: str):
        return effort

    def close(self) -> None:
        self.client.close()

    # Implemented by each provider.
    def _list_models(self, key: str, *, probe: bool = False) -> list[dict]:
        raise NotImplementedError

    def _infer_once(self, key, model, instructions, content, output_type, effort, web_search):
        raise NotImplementedError


class OpenAIKeyProvider(KeyProvider):
    name = 'openai'
    label = 'OpenAI'
    default_model = DEFAULT_MODEL
    fallback_effort = FALLBACK_EFFORT
    NOT_TEXT = re.compile(r'embedding|whisper|tts|dall-e|moderation|transcribe|davinci|babbage', re.I)

    def _error(self, raw: dict, status: int = 0, key: str = '', retry_after: int = 0, event_type: str = '',
               probe: bool = False) -> PlanError:
        error = raw.get('error') or raw
        error = error if isinstance(error, dict) else {}
        code = str(error.get('code') or error.get('type') or '')
        incomplete = raw.get('incomplete_details') or {}
        reason = str(incomplete.get('reason', '')) if isinstance(incomplete, dict) else ''
        diagnostic = {'code': code or reason, 'event_type': event_type}
        if code == 'insufficient_quota' or (status == 429 and 'quota' in code):
            message = 'La cuenta de OpenAI se quedó sin saldo o cupo. Revisa la facturación de la llave y reintenta.'
            if key and not probe:
                self._set_hold(key, 'limit', message, retry_after)
            return PlanError(message, 'limit', retry_after, **diagnostic)
        if status in TRANSIENT_STATUS or code in {'server_error', 'internal_error', 'rate_limit_exceeded',
                                                  'overloaded', 'timeout'}:
            return PlanError('OpenAI está ocupado o no respondió a tiempo.', 'transient', retry_after, **diagnostic)
        if status == 401 or code in {'invalid_api_key', 'invalid_authentication'}:
            message = 'OpenAI no aceptó la llave. Escribe una llave vigente en Ajustes.'
            if key and not probe:
                self._set_hold(key, 'reconnect', message)
            return PlanError(message, 'reconnect', **diagnostic)
        if status == 403:
            return PlanError('La llave de OpenAI no tiene permiso para esta solicitud.', 'ineligible', **diagnostic)
        if status in {400, 404, 413} or code in {'model_not_found', 'invalid_request_error', 'invalid_prompt',
                                                 'context_length_exceeded'}:
            return PlanError('El modelo elegido de OpenAI no admite esta solicitud.', 'model', **diagnostic)
        if reason == 'content_filter' or code in {'content_filter', 'safety_violation'}:
            return PlanError('El servicio pidió revisar este paso antes de continuar.', 'unavailable', **diagnostic)
        if event_type in {'error', 'response.failed', 'response.incomplete'} and not status:
            return PlanError('El análisis se interrumpió. Puedes reintentar el ticket.', 'transient', retry_after,
                             **diagnostic)
        suffix = f' (HTTP {status})' if status else ''
        return PlanError('OpenAI no pudo completar la solicitud' + suffix + '.', **diagnostic)

    @staticmethod
    def _rejects_effort(raw: dict) -> bool:
        error = raw.get('error') if isinstance(raw.get('error'), dict) else raw
        param = str(error.get('param') or '')
        message = str(error.get('message') or '')
        return param in {'reasoning.effort', 'reasoning'} or 'reasoning.effort' in message

    def _list_models(self, key: str, *, probe: bool = False) -> list[dict]:
        try:
            response = self.client.get(OPENAI_API + '/models', headers={'Authorization': 'Bearer ' + key})
        except httpx.HTTPError as exc:
            raise PlanError('No se pudo consultar los modelos de OpenAI.', 'transient') from exc
        if not response.is_success:
            raise self._error(body_of(response), response.status_code, key,
                              retry_seconds(response.headers.get('retry-after')), probe=probe)
        listed = [entry.get('id') for entry in body_of(response).get('data', []) if isinstance(entry, dict)]
        return [{'id': model, 'name': model} for model in sorted(set(filter(None, listed)))
                if isinstance(model, str) and re.fullmatch(r'[\w.:/-]{1,120}', model) and not self.NOT_TEXT.search(model)]

    def _infer_once(self, key, model, instructions, content, output_type, effort, web_search):
        # Same request shape as the ChatGPT plan, authorised by the product's key.
        request = {'model': model, 'reasoning': {'effort': effort}, 'store': False, 'stream': True,
                   'instructions': instructions, 'input': [{'role': 'user', 'content': content}],
                   'text': {'format': {'type': 'json_schema', 'name': output_type.__name__.lower(),
                                       'strict': True, 'schema': strict_schema(output_type)}}}
        if web_search:
            request.update(tools=[{'type': 'web_search', 'search_context_size': 'low'}],
                           include=['web_search_call.action.sources'])
        try:
            with self.client.stream('POST', OPENAI_API + '/responses', json=request,
                                    headers={'Authorization': 'Bearer ' + key}) as response:
                if not response.is_success:
                    response.read()
                    body = body_of(response)
                    error = self._error(body, response.status_code, key, retry_seconds(response.headers.get('retry-after')))
                    error.effort_rejected = response.status_code == 400 and self._rejects_effort(body)
                    raise error
                streamed = []
                for event in decode_event_stream(response.iter_lines()):
                    kind = event.get('type')
                    if kind in {'error', 'response.failed', 'response.incomplete'}:
                        raise self._error(event.get('response') or event, key=key, event_type=kind)
                    if web_search and kind == 'response.output_item.done':
                        streamed.append(event['item'])
                    if kind == 'response.completed':
                        final = event['response']
                        items = final.get('output') or streamed
                        parts = [part for item in items if item.get('type') == 'message' for part in item.get('content', [])]
                        if any(part.get('type') == 'refusal' for part in parts):
                            raise PlanError('OpenAI pidió que revises este paso manualmente.', code='refusal')
                        answer = ''.join(part['text'] for part in parts if part.get('type') == 'output_text')
                        if not answer or len(answer) > MAX_ANSWER:
                            raise PlanError('OpenAI no devolvió una respuesta utilizable.')
                        parsed = output_type.model_validate_json(answer)
                        usage = final.get('usage') or {}
                        measured = {'model': model, 'input_tokens': usage.get('input_tokens'),
                                    'output_tokens': usage.get('output_tokens')}
                        if web_search:
                            sources = [source.get('url') for item in [*items, *streamed]
                                       if item.get('type') == 'web_search_call'
                                       for source in item.get('action', {}).get('sources', [])]
                            return (parsed, list(dict.fromkeys(u for u in sources if isinstance(u, str)))), measured
                        return parsed, measured
        except httpx.HTTPError as exc:
            raise PlanError('Se interrumpió la conexión con OpenAI.', 'transient') from exc
        except (TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, PlanError):
                raise
            raise PlanError('OpenAI devolvió datos incompletos. Revisa el ticket en el panel.') from exc
        raise PlanError('La respuesta de OpenAI terminó antes de confirmarse.', 'transient')


class AnthropicKeyProvider(KeyProvider):
    name = 'anthropic'
    label = 'Claude'
    default_model = ANTHROPIC_DEFAULT_MODEL
    fallback_effort = None  # Without the effort field the model uses its own default level.

    def __init__(self, settings: AjustesIA, client: httpx.Client | None = None):
        super().__init__(settings, client)
        self._no_fallbacks: set[str] = set()

    def _effort(self, effort: str):
        return effort if effort in ANTHROPIC_EFFORTS else 'medium'

    def _headers(self, key: str, beta: str = '') -> dict:
        headers = {'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION, 'content-type': 'application/json'}
        if beta:
            headers['anthropic-beta'] = beta
        return headers

    def _error(self, raw: dict, status: int = 0, key: str = '', retry_after: int = 0, probe: bool = False) -> PlanError:
        error = raw.get('error') if isinstance(raw.get('error'), dict) else {}
        kind = str(error.get('type') or '')
        message = str(error.get('message') or '').casefold()
        details = error.get('details') if isinstance(error.get('details'), dict) else {}
        detail_code = str(details.get('error_code') or '')
        diagnostic = {'code': detail_code or kind}
        if status == 402 or kind == 'billing_error' or detail_code == 'enforced_spend_limit_reached':
            text = 'La cuenta de Claude llegó a su tope de gasto o no tiene saldo. Revisa la facturación de la llave.'
            if key and not probe:
                self._set_hold(key, 'limit', text, retry_after)
            return PlanError(text, 'limit', retry_after, **diagnostic)
        if status in TRANSIENT_STATUS or kind in {'rate_limit_error', 'overloaded_error', 'api_error', 'timeout_error'}:
            return PlanError('Claude está ocupado o no respondió a tiempo.', 'transient', retry_after, **diagnostic)
        if status == 401 or kind == 'authentication_error':
            text = 'Anthropic no aceptó la llave. Escribe una llave vigente en Ajustes.'
            if key and not probe:
                self._set_hold(key, 'reconnect', text)
            return PlanError(text, 'reconnect', **diagnostic)
        if status == 403 or kind == 'permission_error':
            return PlanError('La llave de Claude no tiene permiso para esta solicitud.', 'ineligible', **diagnostic)
        if status in {400, 404, 413} or kind in {'invalid_request_error', 'not_found_error', 'request_too_large'}:
            result = PlanError('El modelo elegido de Claude no admite esta solicitud.', 'model', **diagnostic)
            # Only classification reads the server text; it is never shown nor stored.
            result.effort_rejected = status == 400 and 'effort' in message
            result.fallbacks_rejected = status == 400 and ('fallback' in message or 'anthropic-beta' in message)
            return result
        suffix = f' (HTTP {status})' if status else ''
        return PlanError('Claude no pudo completar la solicitud' + suffix + '.', **diagnostic)

    def _list_models(self, key: str, *, probe: bool = False) -> list[dict]:
        models, after = [], ''
        for _ in range(5):
            params = {'limit': 1000, **({'after_id': after} if after else {})}
            try:
                response = self.client.get(ANTHROPIC_API + '/models', params=params, headers=self._headers(key))
            except httpx.HTTPError as exc:
                raise PlanError('No se pudo consultar los modelos de Claude.', 'transient') from exc
            if not response.is_success:
                raise self._error(body_of(response), response.status_code, key,
                                  retry_seconds(response.headers.get('retry-after')), probe=probe)
            data = body_of(response)
            for entry in data.get('data', []):
                model = entry.get('id') if isinstance(entry, dict) else None
                if isinstance(model, str) and re.fullmatch(r'[\w.:/-]{1,120}', model):
                    name = entry.get('display_name')
                    models.append({'id': model, 'name': name[:120] if isinstance(name, str) and name else model})
            after = data.get('last_id') or ''
            if not data.get('has_more') or not after:
                break
        return list({entry['id']: entry for entry in models}.values())

    def _request(self, model, instructions, content, output_type, effort, web_search) -> dict:
        output = {'format': {'type': 'json_schema', 'schema': anthropic_schema(output_type)}}
        if effort:
            output['effort'] = effort
        request = {'model': model, 'max_tokens': ANTHROPIC_MAX_TOKENS, 'system': instructions,
                   'messages': [{'role': 'user', 'content': anthropic_content(content)}], 'output_config': output}
        if web_search:
            tool = 'web_search_20260209' if ANTHROPIC_DYNAMIC_SEARCH.match(model) else 'web_search_20250305'
            request['tools'] = [{'type': tool, 'name': 'web_search', 'max_uses': 5}]
        return request

    def _infer_once(self, key, model, instructions, content, output_type, effort, web_search):
        request = self._request(model, instructions, content, output_type, effort, web_search)
        with self.lock:
            fallback = model in ANTHROPIC_FALLBACK_MODELS and model not in self._no_fallbacks
        usage = {'model': model, 'input_tokens': 0, 'output_tokens': 0}
        blocks: list[dict] = []
        for _ in range(MAX_CONTINUATIONS + 1):
            body = dict(request)
            if fallback:
                body['fallbacks'] = 'default'
            try:
                response = self.client.post(ANTHROPIC_API + '/messages', json=body,
                                            headers=self._headers(key, ANTHROPIC_FALLBACK_BETA if fallback else ''))
            except httpx.HTTPError as exc:
                raise PlanError('Se interrumpió la conexión con Claude.', 'transient') from exc
            data = body_of(response)
            if not response.is_success:
                error = self._error(data, response.status_code, key, retry_seconds(response.headers.get('retry-after')))
                if fallback and getattr(error, 'fallbacks_rejected', False):
                    # This account or model does not take the refusal fallback: ask without it from now on.
                    with self.lock:
                        self._no_fallbacks.add(model)
                    fallback = False
                    continue
                raise error
            try:
                reported = data.get('usage') or {}
                for field, names in (('input_tokens', ('input_tokens', 'cache_creation_input_tokens',
                                                       'cache_read_input_tokens')), ('output_tokens', ('output_tokens',))):
                    values = [reported.get(name) for name in names]
                    if usage[field] is not None and isinstance(values[0], int):
                        usage[field] += sum(v for v in values if isinstance(v, int))
                    else:
                        usage[field] = None
                if isinstance(data.get('model'), str) and re.fullmatch(r'[\w.:/-]{1,120}', data['model']):
                    usage['model'] = data['model']  # A fallback may have served the turn.
                turn = [block for block in data['content'] if isinstance(block, dict)]
                blocks.extend(turn)
                stop = data.get('stop_reason')
                if stop == 'refusal':
                    raise PlanError('Claude pidió que revises este paso manualmente.', code='refusal')
                if stop == 'max_tokens':
                    raise PlanError('Claude no terminó la respuesta. Puedes reintentar el ticket.', code='max_tokens')
                if stop == 'pause_turn':
                    # A long server-side search paused: send the turn back and the server resumes it.
                    request = {**request, 'messages': [*request['messages'], {'role': 'assistant', 'content': turn}]}
                    continue
                return self._parse(blocks, output_type, web_search), usage
            except (TypeError, KeyError, ValueError) as exc:
                if isinstance(exc, PlanError):
                    raise
                raise PlanError('Claude devolvió datos incompletos. Revisa el ticket en el panel.') from exc
        raise PlanError('La búsqueda de Claude no terminó. Puedes reintentar el ticket.', 'transient')

    @staticmethod
    def _parse(blocks: list[dict], output_type, web_search):
        tools = [index for index, block in enumerate(blocks)
                 if block.get('type') in {'server_tool_use', 'web_search_tool_result'}]
        tail = blocks[tools[-1] + 1:] if tools else blocks
        answer = ''.join(block.get('text', '') for block in tail if block.get('type') == 'text').strip()
        if not answer or len(answer) > MAX_ANSWER:
            raise PlanError('Claude no devolvió una respuesta utilizable.')
        parsed = output_type.model_validate_json(answer)
        if not web_search:
            return parsed
        sources = []
        for block in blocks:
            if block.get('type') == 'web_search_tool_result' and isinstance(block.get('content'), list):
                sources.extend(item.get('url') for item in block['content'] if isinstance(item, dict))
            for citation in block.get('citations') or []:
                if isinstance(citation, dict):
                    sources.append(citation.get('url'))
        return parsed, list(dict.fromkeys(url for url in sources if isinstance(url, str) and url))


PROVIDER_CLASSES = {'openai': OpenAIKeyProvider, 'anthropic': AnthropicKeyProvider}


class SelectorIA:
    """The object the program receives as `plan`. It delegates to the active provider and logs usage."""

    def __init__(self, root: Path, port: int = 8765, *, chatgpt: ChatGPTPlan | None = None,
                 client: httpx.Client | None = None, environ=None, usage: RegistroUso | None = None):
        self.root = Path(root)
        self.chatgpt = chatgpt if chatgpt is not None else ChatGPTPlan(self.root, port)
        self.settings = AjustesIA(self.root, environ)
        self._shared_client = client
        self.providers = {name: cls(self.settings, client) for name, cls in PROVIDER_CLASSES.items()}
        self.usage = usage or RegistroUso(self.root)

    def __getattr__(self, name):
        # Anything else the ChatGPT plan exposes (session_path, redirect…) keeps working unchanged.
        chatgpt = self.__dict__.get('chatgpt')
        if chatgpt is None or name.startswith('__'):
            raise AttributeError(name)
        return getattr(chatgpt, name)

    @property
    def active_name(self) -> str:
        return self.settings.active()

    def active(self):
        name = self.active_name
        return self.chatgpt if name == CHATGPT else self.providers[name]

    def status(self) -> dict:
        return self.active().status()

    def models(self) -> list[dict]:
        return self.active().models()

    def select_model(self, model: str, tier: str | None = None) -> None:
        provider = self.active()
        if provider is self.chatgpt:
            return self.chatgpt.select_model(model)
        return provider.select_model(model, tier or 'normal')

    # The OAuth connection always concerns the ChatGPT plan, whichever provider is active.
    def start(self, new_account: bool = False, consent: bool = False) -> str:
        return self.chatgpt.start(new_account, consent)

    def finish(self, state: str, code: str, issued_client: str, error: str = '') -> None:
        return self.chatgpt.finish(state, code, issued_client, error)

    def disconnect(self) -> bool:
        return self.chatgpt.disconnect()

    def clear_limit(self) -> None:
        self.active().clear_limit()

    def infer(self, instructions, content, output_type, *, effort='medium', web_search=False):
        name = self.active_name
        task, ticket = tarea_actual(output_type)
        started = time.monotonic()
        usage: dict = {}
        outcome = 'ok'
        try:
            if name == CHATGPT:
                return self.chatgpt.infer(instructions, content, output_type, effort=effort, web_search=web_search)
            usage = {'model': self.providers[name].model_for(effort)}
            result, usage = self.providers[name].infer_measured(instructions, content, output_type,
                                                                effort=effort, web_search=web_search)
            return result
        except PlanError as exc:
            outcome = exc.status if re.fullmatch(r'[a-z_]{1,40}', exc.status or '') else 'error'
            raise
        except BaseException:
            outcome = 'error'
            raise
        finally:
            if name == CHATGPT:
                try:
                    usage = {'model': str(self.chatgpt.status().get('model', ''))}
                except (OSError, ValueError, PlanError):
                    usage = {}
            self.usage.record(task=task, ticket=ticket, provider=name, model=usage.get('model', ''), effort=effort,
                              duration_ms=int((time.monotonic() - started) * 1000),
                              input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'),
                              web_search=web_search, outcome=outcome)

    def close(self) -> None:
        self.chatgpt.close()
        if self._shared_client is not None:
            self._shared_client.close()
            return
        for provider in self.providers.values():
            provider.close()


__all__ = ['AnthropicKeyProvider', 'OpenAIKeyProvider', 'SelectorIA', 'KEY_PROVIDERS', 'anthropic_content',
           'anthropic_schema']
