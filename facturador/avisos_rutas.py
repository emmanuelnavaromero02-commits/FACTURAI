"""HTTP routes of the notices (list, read marks, devices, settings, test) and the panel files they need."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Body, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import avisos as avisos_module
from .avisos_push import public_key


class Keys(BaseModel):
    model_config = ConfigDict(extra='ignore')
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=200)


class Subscription(BaseModel):
    model_config = ConfigDict(extra='ignore')
    endpoint: str = Field(min_length=10, max_length=2048)
    keys: Keys
    nombre: str = Field(default='', max_length=60)


class Read(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ids: list[int] = Field(default_factory=list, max_length=500)
    todos: StrictBool = False


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tipos: dict[str, StrictBool] = Field(default_factory=dict)
    canales: dict[str, StrictBool] = Field(default_factory=dict)
    correo_destino: str | None = None


def avisos_router(store, avisos):
    router = APIRouter()
    root = store.root

    def settings_view():
        current = avisos_module.ajustes(root)
        return {**current, 'textos': {'tipos': avisos_module.TIPOS_TEXTO, 'canales': avisos_module.CANALES},
                'correo_disponible': avisos.correo.disponible(),
                'dispositivos': avisos.push.devices()}

    @router.get('/api/avisos')
    def notices(limite: int = Query(default=50, ge=1, le=200)):
        return avisos.lista(limite)

    @router.get('/api/avisos/sin-leer')
    def unread():
        return {'sin_leer': avisos.sin_leer()}

    @router.post('/api/avisos/leidos')
    def mark_read(body: Read):
        return {'sin_leer': avisos.marcar_leidos(body.ids, body.todos)}

    @router.get('/api/avisos/llave')
    def key():
        return {'llave': public_key(root)}

    @router.post('/api/avisos/dispositivos')
    def subscribe(body: Subscription):
        return avisos.push.subscribe(body.endpoint, body.keys.p256dh, body.keys.auth, body.nombre)

    @router.get('/api/avisos/dispositivos')
    def devices():
        return {'dispositivos': avisos.push.devices()}

    @router.delete('/api/avisos/dispositivos/{device}')
    def remove(device: str):
        return {'quitado': avisos.push.remove(device), 'dispositivos': avisos.push.devices()}

    @router.post('/api/avisos/dispositivos/quitar')
    def remove_endpoint(endpoint: str = Body(embed=True, max_length=2048)):
        return {'quitado': avisos.push.remove(endpoint=endpoint), 'dispositivos': avisos.push.devices()}

    @router.get('/api/avisos/ajustes')
    def read_settings():
        return settings_view()

    @router.put('/api/avisos/ajustes')
    def save_settings(body: Settings):
        avisos_module.guardar_ajustes(root, body.model_dump())
        return settings_view()

    @router.post('/api/avisos/prueba')
    async def test():
        if not avisos.push.count():
            raise ValueError('Activa primero los avisos en este dispositivo.')
        return await asyncio.to_thread(avisos.prueba)

    return router


# Published path -> (file in web/, media type). Served by the routes below, not by the cached panel assets.
PIEZAS = {
    '/sw.js': ('sw.js', 'application/javascript'),
    '/avisos.js': ('avisos.js', 'application/javascript'),
    '/avisos.css': ('avisos.css', 'text/css'),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
    '/icono-192.png': ('icono-192.png', 'image/png'),
    '/icono-512.png': ('icono-512.png', 'image/png'),
    '/icono-maskable-512.png': ('icono-maskable-512.png', 'image/png'),
    '/icono-180.png': ('icono-180.png', 'image/png'),
}


def _piece(body: bytes, kind: str, name: str):
    def serve():
        return Response(body, media_type=kind, headers={'Cache-Control': 'no-cache'})
    serve.__name__ = 'pieza_' + name.replace('.', '_').replace('-', '_')
    return serve


def piezas_router(folder: Path):
    """The service worker must live at the site root so it can open any panel link."""
    router = APIRouter()
    for path, (name, kind) in PIEZAS.items():
        source = Path(folder) / name
        if source.is_file():
            router.add_api_route(path, _piece(source.read_bytes(), kind, name), methods=['GET'],
                                 include_in_schema=False)
    return router
