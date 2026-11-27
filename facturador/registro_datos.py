"""Personal data a billing portal may ask for when it requires an account, kept apart from the tax profile.

Everything is optional. The values live in a private file in the data folder and are only read by
``datos_de_registro`` to fill a registration form. They are never sent to the model, written to events or
logged, and a rejected value is never echoed back.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .auth import read_json, write_private

CAMPOS = {
    'nombres': 'el nombre', 'apellido_paterno': 'el apellido paterno', 'apellido_materno': 'el apellido materno',
    'telefono': 'el teléfono', 'fecha_nacimiento': 'la fecha de nacimiento', 'calle': 'la calle',
    'numero_exterior': 'el número exterior', 'numero_interior': 'el número interior', 'colonia': 'la colonia',
    'municipio': 'el municipio o la ciudad', 'estado': 'el estado', 'pais': 'el país',
}
_NAME = re.compile(r"[^\W\d_]+(?:[ '.-][^\W\d_]+)*\.?")
_PLACE = re.compile(r"[\w][\w .,'#°ºª/()-]*")
_NUMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 #/.-]*")
_CONTROL = re.compile(r'[\x00-\x1f\x7f<>]')


def _clean(value) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError
    if _CONTROL.search(value):
        raise ValueError
    return ' '.join(value.split())


class DatosRegistro(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nombres: str = ''
    apellido_paterno: str = ''
    apellido_materno: str = ''
    telefono: str = ''
    fecha_nacimiento: str = ''
    calle: str = ''
    numero_exterior: str = ''
    numero_interior: str = ''
    colonia: str = ''
    municipio: str = ''
    estado: str = ''
    pais: str = ''

    @field_validator('nombres', 'apellido_paterno', 'apellido_materno', mode='before')
    @classmethod
    def valid_name(cls, value):
        value = _clean(value)
        if value and (len(value) > 80 or not _NAME.fullmatch(value)):
            raise ValueError
        return value

    @field_validator('calle', 'colonia', 'municipio', mode='before')
    @classmethod
    def valid_place(cls, value):
        value = _clean(value)
        if value and (len(value) > 120 or not _PLACE.fullmatch(value)):
            raise ValueError
        return value

    @field_validator('estado', 'pais', mode='before')
    @classmethod
    def valid_region(cls, value):
        value = _clean(value)
        if value and (len(value) > 60 or not _NAME.fullmatch(value)):
            raise ValueError
        return value

    @field_validator('numero_exterior', 'numero_interior', mode='before')
    @classmethod
    def valid_number(cls, value):
        value = _clean(value)
        if value and (len(value) > 20 or not _NUMBER.fullmatch(value)):
            raise ValueError
        return value

    @field_validator('telefono', mode='before')
    @classmethod
    def valid_phone(cls, value):
        value = re.sub(r'[\s().-]', '', _clean(value))
        if value and not re.fullmatch(r'\+?\d{10,15}', value):
            raise ValueError
        return value

    @field_validator('fecha_nacimiento', mode='before')
    @classmethod
    def valid_birth(cls, value):
        value = _clean(value)
        if not value:
            return ''
        day = date.fromisoformat(value) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
        if day is None or not date(1900, 1, 1) <= day <= date.today():
            raise ValueError
        return day.isoformat()


def _path(root) -> Path:
    return Path(root) / 'registro.json'


def datos_de_registro(root) -> dict:
    """Saved registration data, every field present ('' when not given). For filling portal forms only."""
    try:
        saved = read_json(_path(root), {})
        return DatosRegistro.model_validate(saved if isinstance(saved, dict) else {}).model_dump()
    except (ValueError, ValidationError):
        return DatosRegistro().model_dump()


def validar(data) -> DatosRegistro:
    """Validate without ever repeating the value: the message names the field only."""
    if not isinstance(data, dict):
        raise ValueError('Revisa los datos para registros.')
    unknown = [key for key in data if key not in CAMPOS]
    if unknown:
        raise ValueError('Revisa los datos para registros.')
    for key, value in data.items():
        try:
            DatosRegistro.model_validate({key: value})
        except ValidationError:
            raise ValueError(f'Revisa {CAMPOS[key]}.') from None
    return DatosRegistro.model_validate(data)


def guardar_datos(root, data) -> dict:
    value = validar(data).model_dump()
    write_private(_path(root), value)
    return value


def registro_router(root):
    router = APIRouter()

    @router.get('/api/registro/datos')
    def read():
        return {'datos': datos_de_registro(root), 'campos': CAMPOS}

    @router.put('/api/registro/datos')
    async def save(request: Request):
        raw = await request.body()
        try:
            if len(raw) > 8192:
                raise ValueError
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError('Revisa los datos para registros.') from None
        return {'datos': guardar_datos(root, body), 'campos': CAMPOS}

    return router
