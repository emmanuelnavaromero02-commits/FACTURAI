"""HTTP routes for the standing permissions of ``permisos.py``. Each change leaves a dated audit event."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body

from .permisos import PERMISOS, estado, guardar

AUDIT_KIND = 'permiso'


def permisos_router(store):
    router = APIRouter()
    root = store.root

    @router.get('/api/permisos')
    def read():
        return estado(root)

    @router.put('/api/permisos/{nombre}')
    def change(nombre: str, activo: bool = Body(embed=True, strict=True)):
        if nombre not in PERMISOS:
            raise ValueError('Permiso desconocido.')
        result = guardar(root, nombre, activo)
        when = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        # Only the permission name, the new value and the date: no personal data.
        store.event(None, AUDIT_KIND, f"Permiso {nombre} {'activado' if activo else 'desactivado'} el {when}.")
        return result

    return router
