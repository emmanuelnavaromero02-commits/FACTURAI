"""Read-only recovery of an already-issued BP invoice from its official email."""

import asyncio
import hashlib
import re
from datetime import date, timedelta
from email.utils import getaddresses
from pathlib import Path
from tempfile import TemporaryDirectory

from .bp_ticket import BPEvidence, at_form
from .cfdi import collect_documents, find_valid_pair
from .documents import save_candidate
from .gmail import GmailConnection, cfdi_parts, read_selected_message, search_tickets
from .models import money


SENDER = 'facturacion@grupocentra.mx'


def _mail_query(receipt) -> str:
    day = date.fromisoformat(receipt.date)
    # Gmail dates are evaluated in its mailbox timezone; include either side of
    # the sale and allow a week for the existing invoice to reach the inbox.
    start = day - timedelta(days=1)
    end = day + timedelta(days=8)
    return (f'from:{SENDER} has:attachment '
            f'after:{start:%Y/%m/%d} before:{end:%Y/%m/%d}')


def _exact_mailbox(msg, email: str) -> bool:
    senders = [address.casefold() for _, address in getaddresses(msg.get_all('From', []))]
    recipients = [address.casefold() for _, address in getaddresses(msg.get_all('To', []))]
    # Gmail prepends its own Authentication-Results. An arbitrary later header
    # in the MIME payload must not override a failed result from Google.
    result = msg.get('Authentication-Results', '')
    authenticated = (result.strip().casefold().startswith('mx.google.com;')
                     and re.search(r'\bdkim=pass\b[^;]*\bheader\.i=@grupocentra\.mx\b', result, re.I)
                     and re.search(r'\bspf=pass\b', result, re.I))
    return senders == [SENDER] and email.casefold() in recipients and authenticated


def _candidate(msg, folder: Path, profile, receipt, expected_uuid: str):
    if not _exact_mailbox(msg, profile.email):
        return None
    attachments = list(cfdi_parts(msg))
    if not attachments or len(attachments) > 20:
        return None
    paths = []
    for index, (filename, data) in enumerate(attachments):
        suffix = Path(filename).suffix.lower()
        target = folder / f'{index:02d}-{hashlib.sha256(data).hexdigest()}{suffix}'
        target.write_bytes(data)
        paths.append(target)
    try:
        files = collect_documents(paths, folder / 'extraidos')
        result, xml, pdf = find_valid_pair(files, profile, receipt, expected_uuid)
        return result['uuid'], xml.read_bytes(), pdf.read_bytes()
    except (OSError, ValueError):
        return None


def _locate_existing(root: Path, profile, receipt, expected_uuid: str):
    messages = search_tickets(root, _mail_query(receipt))
    # search_tickets caps results at ten. At that boundary we cannot prove
    # uniqueness, so leave the browser session available for manual recovery.
    if len(messages) >= 10:
        return None
    found = {}
    with TemporaryDirectory(dir=root, prefix='bp-correo-') as scratch:
        base = Path(scratch)
        for index, metadata in enumerate(messages):
            msg = read_selected_message(root, metadata['id'])
            folder = base / str(index)
            folder.mkdir(mode=0o700)
            candidate = _candidate(msg, folder, profile, receipt, expected_uuid)
            if candidate:
                uuid, xml, pdf = candidate
                prior = found.setdefault(uuid, (xml, pdf))
                if prior != (xml, pdf):
                    return None
        return next(iter(found.values())) if len(found) == 1 else None


async def recover_bp_reprint(runner, page, key: str, profile, receipt, evidence: BPEvidence) -> bool:
    """Recover the existing CFDI only; this path never submits a billing form."""
    if (not at_form(page, receipt) or not evidence
            or evidence.ticket_number != receipt.ticket_number
            or evidence.payment_code != receipt.payment_code
            or evidence.amount != money(receipt.amount)):
        return False
    if await runner.recover(key, profile):
        return True
    if GmailConnection(runner.store.root).status() != 'connected':
        runner.store.event(key, 'bp_reprint_unavailable',
                           'BP indica que el ticket ya fue facturado. Conecta Gmail de solo lectura o adjunta el PDF y XML existentes para cotejarlos.')
        return False
    try:
        existing = await asyncio.to_thread(_locate_existing, runner.store.root, profile, receipt,
                                           runner.store.get(key)['uuid'] or '')
    except (OSError, ValueError):
        runner.store.event(key, 'bp_reprint_unavailable',
                           'No se pudo consultar Gmail. Reintenta la recuperación o adjunta el PDF y XML existentes.')
        return False
    if existing is None:
        runner.store.event(key, 'bp_reprint_unavailable',
                           'No hay una pareja PDF/XML única del mismo ticket BP en Gmail. Adjunta los comprobantes existentes.')
        return False
    xml, pdf = existing
    save_candidate(runner.store, key, 'bp-existente.xml', xml)
    save_candidate(runner.store, key, 'bp-existente.pdf', pdf)
    runner.store.event(key, 'bp_reprint_mail', 'Se recuperó una pareja PDF/XML del correo oficial de BP, sin nueva emisión.')
    return await runner.recover(key, profile)
