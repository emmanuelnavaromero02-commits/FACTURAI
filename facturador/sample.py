from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont


def sample_ticket() -> bytes:
    image = Image.new('RGB', (900, 810), '#fdf9ee')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    title = ImageFont.load_default(size=43)
    draw.text((70, 60), 'PLAZA DE PEAJE · EJEMPLO', font=title, fill='#19251f')
    draw.line((70, 130, 830, 130), fill='#9aa89e', width=2)
    rows = ['CAPUFE · Terminal de demostración', 'Fecha            2026-11-21   18:02',
            'Folio            DEMO-000001', 'Clase            A1', 'Forma de pago    Efectivo',
            'Total            $50.00 MXN', 'Código           EJEMPLO2517000000',
            'Ticket sintético para practicar la revisión']
    for index, line in enumerate(rows):
        draw.text((70, 172 + index * 61), line, font=font, fill='#19251f')
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()
