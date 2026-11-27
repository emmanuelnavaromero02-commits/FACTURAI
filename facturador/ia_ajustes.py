"""Configuración de la IA de pago por uso: proveedor activo, llaves cifradas y modelo por nivel.

La llave se guarda cifrada (misma bóveda Fernet que las contraseñas de portal y el correo) y
nunca vuelve por la API: solo «configurada» y sus últimos 4 caracteres. Si el servidor fija la
llave por variable de entorno, esa manda y el panel no la puede cambiar.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import PlanError, read_json, write_private
from . import secretos, uso_ia

CHATGPT = 'chatgpt'
KEY_PROVIDERS = {'openai': 'OpenAI (llave de API)', 'anthropic': 'Claude de Anthropic (llave de API)'}
PROVIDERS = {CHATGPT: 'Plan de ChatGPT', **KEY_PROVIDERS}
# Product environment variables: when present they win over the panel.
ENV_KEYS = {'openai': 'FACTURAI_OPENAI_API_KEY', 'anthropic': 'FACTURAI_ANTHROPIC_API_KEY'}
TIERS = ('rapido', 'normal', 'dificil')
MODEL_ID = re.compile(r'[\w.:/-]{1,120}')
SETTINGS_LOCK = threading.RLock()


def tier_for(effort: str) -> str:
    """Reasoning level the code asks for → the owner's model tier."""
    return {'low': 'rapido', 'medium': 'normal'}.get(effort, 'dificil')


def valid_key(value) -> str:
    if not isinstance(value, str):
        raise ValueError('Escribe la llave completa.')
    value = value.strip()
    if not 20 <= len(value) <= 400 or not re.fullmatch(r'[\x21-\x7e]+', value):
        raise ValueError('La llave no tiene el formato esperado. Cópiala completa, sin espacios.')
    return value


class AjustesIA:
    """Settings file `auth/ia/ajustes.json` and its separate vault key `auth/ia/vault-key.json`."""

    def __init__(self, root: Path, environ=None):
        self.folder = Path(root) / 'auth' / 'ia'
        self.path = self.folder / 'ajustes.json'
        self.key_file = self.folder / 'vault-key.json'
        self.environ = os.environ if environ is None else environ

    # -- raw record
    def _read(self) -> dict:
        try:
            record = read_json(self.path, {})
        except (ValueError, OSError):
            record = {}
        return record if isinstance(record, dict) else {}

    def _write(self, record: dict) -> None:
        write_private(self.path, record)

    def _vault(self, create: bool) -> Fernet:
        # With a master key configured the vault key itself is stored sealed.
        key = secretos.read_json(self.key_file, None, purpose=secretos.IA_VAULT)
        if key is None:
            if not create:
                raise ValueError('Falta la clave privada del almacén de la IA. Vuelve a escribir la llave.')
            key = {'key': Fernet.generate_key().decode('ascii')}
            secretos.write_json(self.key_file, key, purpose=secretos.IA_VAULT)
        return Fernet(key['key'].encode('ascii'))

    # -- keys
    def env_key(self, provider: str) -> str:
        value = self.environ.get(ENV_KEYS.get(provider, ''), '') or ''
        try:
            return valid_key(value) if value.strip() else ''
        except ValueError:
            return ''

    def key(self, provider: str) -> str:
        """The plaintext key, only for building a request header. Never logged nor returned."""
        if provider not in KEY_PROVIDERS:
            return ''
        if env := self.env_key(provider):
            return env
        with SETTINGS_LOCK:
            saved = self._read().get('llaves', {}).get(provider, {})
            if not isinstance(saved, dict) or not saved.get('secret'):
                return ''
            try:
                return self._vault(create=False).decrypt(saved['secret'].encode('ascii')).decode('ascii')
            except (InvalidToken, ValueError, UnicodeError, KeyError, TypeError):
                return ''

    def key_info(self, provider: str) -> dict:
        if env := self.env_key(provider):
            return {'configurada': True, 'ultimos': env[-4:], 'origen': 'entorno', 'editable': False}
        saved = self._read().get('llaves', {}).get(provider, {})
        if isinstance(saved, dict) and saved.get('secret') and self.key(provider):
            return {'configurada': True, 'ultimos': str(saved.get('ultimos', ''))[-4:], 'origen': 'panel',
                    'editable': True, 'guardada': saved.get('guardada', 0)}
        return {'configurada': False, 'ultimos': '', 'origen': '', 'editable': True}

    def _require_editable(self, provider: str) -> None:
        if provider not in KEY_PROVIDERS:
            raise ValueError('Elige un proveedor con llave: OpenAI o Claude.')
        if self.env_key(provider):
            raise ValueError('Esta llave la fija el servidor del producto; no se puede cambiar desde el panel.')

    def save_key(self, provider: str, key: str) -> None:
        self._require_editable(provider)
        key = valid_key(key)
        with SETTINGS_LOCK:
            record = self._read()
            vault = self._vault(create=True)
            keys = record.setdefault('llaves', {})
            keys[provider] = {'secret': vault.encrypt(key.encode('ascii')).decode('ascii'),
                              'ultimos': key[-4:], 'guardada': int(time.time())}
            self._write(record)

    def remove_key(self, provider: str) -> None:
        self._require_editable(provider)
        with SETTINGS_LOCK:
            record = self._read()
            record.get('llaves', {}).pop(provider, None)
            if record.get('proveedor') == provider:
                record['proveedor'] = CHATGPT  # Without a key the program falls back to the plan.
            self._write(record)
            if not record.get('llaves'):
                secretos.remove(self.key_file, purpose=secretos.IA_VAULT)

    # -- active provider
    def active(self) -> str:
        chosen = self._read().get('proveedor')
        if chosen in KEY_PROVIDERS:
            return chosen if self.key(chosen) else CHATGPT
        if chosen == CHATGPT:
            return CHATGPT
        # Nothing chosen: a product key fixed by the server is the deployment's provider.
        fixed = [name for name in KEY_PROVIDERS if self.env_key(name)]
        return fixed[0] if len(fixed) == 1 else CHATGPT

    def choose(self, provider: str) -> None:
        if provider not in PROVIDERS:
            raise ValueError('Proveedor de IA desconocido.')
        if provider in KEY_PROVIDERS and not self.key(provider):
            raise ValueError('Guarda primero la llave de este proveedor.')
        with SETTINGS_LOCK:
            record = self._read()
            record['proveedor'] = provider
            self._write(record)

    # -- models per tier
    def models(self, provider: str) -> dict:
        saved = self._read().get('modelos', {}).get(provider, {})
        saved = saved if isinstance(saved, dict) else {}
        return {tier: saved[tier] for tier in TIERS if isinstance(saved.get(tier), str) and MODEL_ID.fullmatch(saved[tier])}

    def set_models(self, provider: str, chosen: dict) -> None:
        if provider not in KEY_PROVIDERS:
            raise ValueError('Elige un proveedor con llave: OpenAI o Claude.')
        with SETTINGS_LOCK:
            record = self._read()
            current = record.setdefault('modelos', {}).setdefault(provider, {})
            for tier, model in chosen.items():
                if tier not in TIERS:
                    raise ValueError('Los niveles son rápido, normal y difícil.')
                if model:
                    if not MODEL_ID.fullmatch(model):
                        raise ValueError('Elige un modelo de la lista.')
                    current[tier] = model
                else:
                    current.pop(tier, None)
            self._write(record)


# ------------------------------------------------------------------ routes

class ProviderChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    proveedor: str = Field(min_length=1, max_length=40)


class TierModels(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rapido: str | None = Field(default=None, max_length=120)
    normal: str | None = Field(default=None, max_length=120)
    dificil: str | None = Field(default=None, max_length=120)


async def _key_body(request: Request) -> str:
    """Parse by hand so a rejected body is never echoed back (it carries the key)."""
    raw = await request.body()
    try:
        if len(raw) > 2048:
            raise ValueError
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {'llave'}:
            raise ValueError
        return valid_key(body['llave'])
    except (ValueError, UnicodeError, TypeError):
        raise ValueError('La llave no tiene el formato esperado. Cópiala completa, sin espacios.') from None


def ia_status(selector) -> dict:
    settings = selector.settings
    active = settings.active()
    providers = []
    for name, label in PROVIDERS.items():
        entry = {'id': name, 'nombre': label, 'activo': name == active}
        if name in KEY_PROVIDERS:
            provider = selector.providers[name]
            entry.update(llave=settings.key_info(name), modelos=provider.tier_models(),
                         estado=provider.status()['status'])
        else:
            entry['estado'] = selector.chatgpt.status()['status']
        providers.append(entry)
    return {'activo': active, 'proveedores': providers, 'niveles': list(TIERS)}


def ia_router(root: Path, selector) -> APIRouter:
    router = APIRouter()
    checking = asyncio.Lock()

    def provider(name: str):
        if name not in KEY_PROVIDERS:
            raise ValueError('Elige un proveedor con llave: OpenAI o Claude.')
        return selector.providers[name]

    @router.get('/api/ia')
    def status():
        return ia_status(selector)

    @router.put('/api/ia/llave/{name}')
    async def save_key(name: str, request: Request):
        key = await _key_body(request)
        target = provider(name)
        selector.settings._require_editable(name)
        if checking.locked():
            raise ValueError('Ya se está probando una llave.')
        async with checking:
            try:
                await asyncio.to_thread(target.check_key, key)
            except PlanError as exc:
                raise ValueError(str(exc)) from None
            await asyncio.to_thread(selector.settings.save_key, name, key)
            target.clear_limit()
        return ia_status(selector)

    @router.delete('/api/ia/llave/{name}')
    def remove_key(name: str):
        provider(name)
        selector.settings.remove_key(name)
        return ia_status(selector)

    @router.put('/api/ia/proveedor')
    def choose(body: ProviderChoice):
        selector.settings.choose(body.proveedor)
        return ia_status(selector)

    @router.get('/api/ia/modelos/{name}')
    async def models(name: str):
        target = provider(name)
        return {'modelos': await asyncio.to_thread(target.models), 'elegidos': target.tier_models()}

    @router.put('/api/ia/modelos/{name}')
    async def set_models(name: str, body: TierModels):
        target = provider(name)
        chosen = {tier: value for tier, value in body.model_dump().items() if value is not None}
        if any(chosen.values()):
            listed = {entry['id'] for entry in await asyncio.to_thread(target.models)}
            if any(model and model not in listed for model in chosen.values()):
                raise ValueError('El modelo no aparece en la lista de este proveedor.')
        selector.settings.set_models(name, chosen)
        return ia_status(selector)

    @router.get('/api/ia/precios')
    def prices():
        return uso_ia.leer_precios(root)

    @router.put('/api/ia/precios')
    async def save_prices(request: Request):
        raw = await request.body()
        if len(raw) > 65_536:
            raise ValueError('La tabla de precios es demasiado grande.')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError('La tabla de precios no es un JSON válido.') from None
        return uso_ia.guardar_precios(root, data)

    @router.get('/api/ia/uso')
    def usage(periodo: str = 'todo'):
        from .medicion import period_start
        return uso_ia.resumen_uso(selector.usage, period_start(periodo))

    return router
