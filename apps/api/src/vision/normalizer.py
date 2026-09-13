import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def normalize_amount(value: Any) -> Optional[Decimal]:
    """
    Normaliza montos monetarios a Decimal con 2 decimales (Regla 8).
    Soporta entradas como:
      - '1,284.50'
      - '$1284.50'
      - '1 284,50'
      - '$ 1.284,50 MXN'
      - 1284.5
      - Decimal('1284.50')
    """
    if value is None:
        return None

    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))

    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(Decimal("0.01"))

    s = str(value).strip()
    if not s:
        return None

    # Remover símbolos de moneda y texto como MXN, USD
    s = re.sub(r"[^\d.,\s]", "", s).strip()

    # Si contiene espacios como separador (ej: '1 284,50' o '1 284.50')
    if " " in s:
        s = s.replace(" ", "")

    # Determinar si la coma o el punto es el separador decimal
    if "," in s and "." in s:
        # Si la coma está antes del punto (ej: '1,284.50') -> coma es miles, punto es decimal
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            # Ej: '1.284,50' -> punto es miles, coma es decimal
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # Solo hay coma (ej: '1284,50' o '1,284')
        parts = s.split(",")
        if len(parts[-1]) == 2:
            # Dos decimales -> coma decimal
            s = s.replace(",", ".")
        else:
            # Miles sin decimales
            s = s.replace(",", "")

    try:
        dec = Decimal(s)
        return dec.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def normalize_date(value: Any) -> Optional[date]:
    """
    Normaliza fechas de compra en formatos mexicanos preferentes (DD/MM/AAAA).
    Si el formato es ambiguo (ej: 04/05/2026), prefiere DD/MM (4 de mayo) sobre MM/DD.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    s = str(value).strip()
    if not s:
        return None

    # Formato ISO: AAAA-MM-DD
    iso_match = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if iso_match:
        try:
            y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
            return date(y, m, d)
        except ValueError:
            pass

    # Formatos tradicionales mexicanos: DD/MM/AAAA o DD-MM-AA
    dmy_match = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if dmy_match:
        try:
            d = int(dmy_match.group(1))
            m = int(dmy_match.group(2))
            y = int(dmy_match.group(3))
            if y < 100:
                y += 2000 if y < 50 else 1900
            return date(y, m, d)
        except ValueError:
            pass

    # Intento con datetime.fromisoformat
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def normalize_time(value: Any) -> Optional[time]:
    """Normaliza la hora del ticket a time (HH:MM:SS o HH:MM)."""
    if value is None:
        return None

    if isinstance(value, time):
        return value

    if isinstance(value, datetime):
        return value.time()

    s = str(value).strip()
    if not s:
        return None

    time_match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if time_match:
        try:
            h = int(time_match.group(1))
            m = int(time_match.group(2))
            sec = int(time_match.group(3)) if time_match.group(3) else 0
            if "pm" in s.lower() and h < 12:
                h += 12
            elif "am" in s.lower() and h == 12:
                h = 0
            return time(h, m, sec)
        except ValueError:
            pass

    return None
