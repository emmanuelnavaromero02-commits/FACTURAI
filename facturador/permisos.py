"""Standing permissions the owner grants once instead of confirming every ticket."""
from __future__ import annotations

import time
from pathlib import Path

from .auth import read_json, write_private

# name -> short description shown in Ajustes. Everything is off until the owner turns it on.
PERMISOS = {
    'registrarme_en_portales': 'Crear mi cuenta cuando un portal de facturación lo exija, con los datos que dejé.',
    'aceptar_terminos_de_registro': 'Aceptar los términos y el aviso de privacidad al crear esa cuenta.',
    'pedir_por_whatsapp': 'Pedir la factura por WhatsApp al comercio sin confirmarlo en cada ticket.',
    'pedir_por_correo': 'Pedir la factura por correo al comercio sin confirmarlo en cada ticket.',
    'leer_mis_fotos_de_whatsapp': 'Tomar como tickets las fotos que me mando a mi propio chat de WhatsApp.',
}


def _path(root: Path) -> Path:
    return Path(root) / 'permisos.json'


def _datos(root: Path) -> dict:
    datos = read_json(_path(root), {})
    return datos if isinstance(datos, dict) else {}


def permitido(root: Path, nombre: str) -> bool:
    """False unless the owner turned this permission on. Unknown names are never allowed."""
    if nombre not in PERMISOS:
        return False
    registro = _datos(root).get(nombre)
    return isinstance(registro, dict) and registro.get('activo') is True


def guardar(root: Path, nombre: str, activo: bool) -> list[dict]:
    if nombre not in PERMISOS:
        raise ValueError('Permiso desconocido.')
    datos = _datos(root)
    datos[nombre] = {'activo': activo is True, 'desde': int(time.time())}
    write_private(_path(root), datos)
    return estado(root)


def estado(root: Path) -> list[dict]:
    datos = _datos(root)
    return [{'nombre': nombre, 'texto': texto,
             'activo': isinstance(datos.get(nombre), dict) and datos[nombre].get('activo') is True,
             'desde': datos[nombre].get('desde') if isinstance(datos.get(nombre), dict) else None}
            for nombre, texto in PERMISOS.items()]
