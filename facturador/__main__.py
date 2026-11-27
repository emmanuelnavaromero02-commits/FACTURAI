import argparse
import getpass
import os
import sys

import uvicorn
from dotenv import load_dotenv

from . import secretos
from .app import BASE, create_app
from .entrada import NEEDS_KEY, data_root, open_accounts, open_entrance
from .usuarios import valid_name, valid_password


def serve(port: int) -> None:
    container = os.getenv('FACTURADOR_CONTAINER') == '1'
    host = '0.0.0.0' if container else '127.0.0.1'
    # The mode and the master key may also live in .env, next to the data folder setting.
    load_dotenv(BASE / '.env')
    try:
        entrance = open_entrance(port=port)
        if entrance is None:
            # Local mode: one person and no accounts. A master key is optional here.
            secretos.load_from_env(data_root())
            secretos.verify(data_root())
    except ValueError as exc:
        raise SystemExit('FACTURAI no puede arrancar. ' + str(exc)) from None
    if entrance is None:
        # Disable access logs so OAuth callbacks never print authorization codes.
        uvicorn.run(create_app(port=port), host=host, port=port, workers=1, access_log=False)
    else:
        # Behind the TLS proxy: its address headers are trusted only from the proxies named here.
        uvicorn.run(entrance, host=host, port=port, workers=1, access_log=False, server_header=False,
                    proxy_headers=True, forwarded_allow_ips=os.getenv('FACTURADOR_PROXIES') or '127.0.0.1')


def create_admin(name: str) -> None:
    """First administrator. The password is typed at the keyboard, never passed as an argument or a variable."""
    load_dotenv(BASE / '.env')
    if not sys.stdin.isatty():
        raise SystemExit('Ejecuta esta orden en una terminal: la contraseña se escribe con el teclado y no se muestra.')
    try:
        root = data_root()
        if not secretos.load_from_env(root):
            raise ValueError(NEEDS_KEY)
        secretos.verify(root)
        accounts = open_accounts(root)
        name = valid_name(name or input('Usuario del administrador: '))
        password = getpass.getpass('Contraseña (mínimo 12 caracteres, no se muestra): ')
        valid_password(password, name)
        if getpass.getpass('Repite la contraseña: ') != password:
            raise ValueError('Las contraseñas no coinciden.')
        account = accounts.create_user(name, password, role='admin')
    except ValueError as exc:
        raise SystemExit('No se creó la cuenta. ' + str(exc)) from None
    print('Cuenta de administrador creada: ' + account['nombre'])


def create_key(target: str) -> None:
    load_dotenv(BASE / '.env')
    try:
        path = secretos.create_key_file(target, data_root())
    except (ValueError, OSError) as exc:
        raise SystemExit('No se creó la llave. ' + (str(exc) if isinstance(exc, ValueError) else 'Revisa la ruta.')) from None
    print(f'Llave maestra creada en {path}\n'
          f'Indica ese archivo en {secretos.FILE_VARIABLE} y guarda una copia en otro lugar seguro: '
          'sin ella no se pueden abrir los secretos guardados.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m facturador', description='Facturador personal de CFDI')
    parser.add_argument('--port', type=int, default=8765)
    commands = parser.add_subparsers(dest='command', metavar='orden')
    admin = commands.add_parser('crear-admin', help='Crea la cuenta de un administrador; la contraseña se pide por teclado.')
    admin.add_argument('--usuario', default='', help='Nombre de la cuenta. Si falta, se pregunta.')
    key = commands.add_parser('crear-llave', help='Crea el archivo de la llave maestra, fuera de la carpeta de datos.')
    key.add_argument('archivo', help='Archivo nuevo donde se guardará la llave.')
    args = parser.parse_args(argv)
    if args.command == 'crear-admin':
        create_admin(args.usuario)
    elif args.command == 'crear-llave':
        create_key(args.archivo)
    else:
        serve(args.port)


if __name__ == '__main__':
    main()
