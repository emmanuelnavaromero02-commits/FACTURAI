"""Retry only the initial portal GET and recover an existing ticket without submitting a form."""
import asyncio
import re
from datetime import date, timedelta

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout

from .catalog import allowed_url
from .cfdi import UUID_RE
from .gmail import GmailConnection, recover_attachments
from .models import Receipt
from .read_http import retry_seconds


FINAL = {'verified', 'duplicate', 'historical'}
RECOVERY = {'issuing', 'issued_waiting_files', 'uncertain'}
REOBSERVE = {'unknown', 'blocked'}
TRANSIENT = re.compile(r'net::ERR_(?:TIMED_OUT|CONNECTION_(?:RESET|CLOSED|TIMED_OUT|REFUSED)|NETWORK_CHANGED|INTERNET_DISCONNECTED|NAME_NOT_RESOLVED)\b')


class RecoveryRequested(Exception):
    """A live paused session must switch to document recovery."""


def may_have_issued(row):
    return row['status'] in RECOVERY or bool(row['issue_attempted'] or row['issuance_seen'] or row['uuid'])


def retry_action(row, pending=None):
    if row['historical'] or row['status'] in FINAL:
        return None
    if pending:
        if pending['kind'] not in REOBSERVE:
            return None
        return 'recover' if may_have_issued(row) else 'resume'
    if row['status'] in {'running', 'queued'}:
        return None
    if may_have_issued(row) or row['human_exposed']:
        return 'recover'
    if row['status'] in {'blocked', 'ready'} and row['billing_profile']:
        return 'queue'
    return None


async def open_ticket_portal(page, url, receipt, challenge, store, key):
    for delay in (1, 3, None):
        try:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=20_000)
        except BrowserError as exc:
            if page.is_closed() or not (isinstance(exc, BrowserTimeout) or TRANSIENT.search(str(exc))):
                raise
            response = None
            failure = 'Se interrumpió la conexión al abrir el portal.'
        else:
            if response and response.status in {404, 410}:
                from .portal_discovery import review_missing_portal
                await review_missing_portal(page, response, url, receipt, challenge, store, key)
            if not response or response.status not in {403, 408, 429, 500, 502, 503, 504}:
                return
            failure = f'El portal respondió HTTP {response.status} al abrir el ticket.'
        if allowed_url(page.url, receipt.provider, receipt.portal_url):
            try:
                snapshot = await page.locator('body').aria_snapshot(timeout=2500)
                if await challenge(page, snapshot):
                    return
            except BrowserError:
                pass
        if response and response.status == 403:
            raise ValueError(failure)
        after = retry_seconds(response.headers.get('retry-after')) if response else 0
        if delay is None or after > 8:
            from .receipt_evidence import alternate_portal_note
            note = await asyncio.to_thread(alternate_portal_note, store.get(key))
            raise ValueError(' '.join(filter(None, (failure, note))))
        wait = max(delay, after)
        detail = f'{failure} Se repetirá la apertura en {wait} s.'
        store.running_message(key, detail)
        store.event(key, 'navigation_retry', detail)
        await asyncio.sleep(wait)


def mail_query(row, receipt):
    uuid = row['uuid'] or ''
    if UUID_RE.fullmatch(uuid):
        return f'has:attachment "{uuid}"'
    try:
        earliest = date.fromisoformat(receipt.date) - timedelta(days=1)
    except (TypeError, ValueError, OverflowError):
        return ''
    for code in [*receipt.billing_codes, receipt.ticket_number]:
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,63}', code or ''):
            return f'has:attachment after:{earliest:%Y/%m/%d} "{code}"'
    return ''


def ticket_in_result(snapshot, row, receipt):
    identifiers = [row['uuid'], receipt.ticket_number, *receipt.billing_codes]
    return any(value and len(value) >= 6 and re.search(r'(?<![\w-])' + re.escape(value) + r'(?![\w-])', snapshot, re.I)
               for value in identifiers)


async def recover_ticket(runner, key, profile):
    store = runner.store
    row = store.get(key)
    if row['status'] in FINAL or await runner.recover(key, profile):
        return
    receipt = Receipt.model_validate(row['receipt'])
    notes = []
    page = runner.viewer.page
    if (page and runner.page_job.get(page) == key and not page.is_closed()
            and allowed_url(page.url, receipt.provider, receipt.portal_url)):
        # An existing page may still show a receipt even after the worker ended.
        # Only inspect its text; no click, form submission, credential or screenshot.
        from .runner import SUCCESS, already_billed
        try:
            if not runner.viewer.dialog:
                snapshot = await page.locator('body').aria_snapshot(timeout=4000)
                if (SUCCESS.search(snapshot) or already_billed(snapshot)) and ticket_in_result(snapshot, row, receipt):
                    store.confirm_issued(key)
                    ids = {v.upper() for v in UUID_RE.findall(snapshot)}
                    if len(ids) == 1 and not row['uuid']:
                        store.update(key, uuid=ids.pop())
                    notes.append('El portal muestra que el ticket fue facturado.')
        except BrowserError:
            notes.append('La pestaña anterior dejó de responder.')
    if receipt.provider == 'capufe':
        from .capufe_recovery import recover_capufe
        report = await recover_capufe(store, key, receipt, profile)
        if report:
            notes.append(report)
        if await runner.recover(key, profile, notes=notes):
            return
    query = mail_query(store.get(key), receipt)
    if GmailConnection(store.root).status() == 'connected' and query:
        try:
            await asyncio.to_thread(recover_attachments, store.root, key, query)
        except ValueError:
            notes.append('Gmail no completó la consulta de este ticket.')
        else:
            if await runner.recover(key, profile):
                return
            notes.append('Gmail no devolvió una pareja PDF y XML verificable para este ticket.')
    elif query:
        notes.append('Vincula Gmail en Ajustes para buscar los archivos por folio.')
    notes.append('Faltan comprobantes cotejados. También puedes adjuntar el PDF y el XML en este expediente.')
    store.finish_recovery(key, ' '.join(notes))
    store.event(key, 'recovery_checked', 'Consulta de comprobantes sin envío de una solicitud de emisión.')
