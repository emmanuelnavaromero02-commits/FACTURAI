import argparse
import os

import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description='Facturador personal de CFDI')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    container = os.getenv('FACTURADOR_CONTAINER') == '1'
    host = '0.0.0.0' if container else '127.0.0.1'
    # Disable access logs so OAuth callbacks never print authorization codes.
    uvicorn.run(create_app(port=args.port), host=host, port=args.port, workers=1, access_log=False)


if __name__ == '__main__':
    main()
