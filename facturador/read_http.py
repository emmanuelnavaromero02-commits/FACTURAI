"""Bounded retries for an idempotent external lookup."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil
import time

import httpx


class ServiceWait(ValueError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f'El servicio pidió esperar {seconds} segundos antes de repetir la consulta.')


def retry_seconds(header):
    if not header:
        return 0
    if header.isascii() and header.isdigit() and len(header) <= 10:
        return int(header)
    try:
        when = parsedate_to_datetime(header)
        return max(0, ceil((when - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return 0


def read_with_retry(lookup):
    for delay in (1, 2, None):
        try:
            return lookup()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise
            wait = retry_seconds(exc.response.headers.get('Retry-After'))
            if wait > 4 or (wait and delay is None):
                raise ServiceWait(wait) from exc
            if delay is None:
                raise
        except httpx.TransportError:
            wait = 0
            if delay is None:
                raise
        time.sleep(max(delay, wait))
