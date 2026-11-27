"""Ask a merchant for the invoice by email: one fixed message per ticket, recorded before it is sent."""
from __future__ import annotations

import secrets
import smtplib
import ssl
import threading
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import httpx

from . import buzon, gmail, outlook
from .db import now
from .ingest import MAX_UPLOAD, validate_document
from .models import Profile, Receipt
from .permisos import permitido

SEND_LOCK = threading.Lock()
REFUSED = 'El servidor de correo rechazó la dirección del comercio. Revisa el correo escrito.'
SENDER_REFUSED = 'El servidor de correo no aceptó enviar desde tu cuenta. Revisa la cuenta en Ajustes.'
NO_SENDER = ('Conecta en Ajustes una cuenta de correo que pueda enviar '
             '(servidor de salida, o Gmail u Outlook con permiso de envío).')
OUTLOOK_SENDING = ('Microsoft no dejó enviar desde tu correo de Outlook. Vuelve a dar el permiso de envío en Ajustes; '
                   'en una cuenta de empresa lo activa quien administra el correo.')
UNCERTAIN = ('El envío pudo haber llegado: revisa la carpeta de enviados de tu correo. '
             'La respuesta del comercio se buscará sola; no se repetirá la solicitud.')


def create_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS mail_requests (
        job_id TEXT PRIMARY KEY, address TEXT NOT NULL, reference TEXT NOT NULL UNIQUE, channel TEXT NOT NULL,
        state TEXT NOT NULL, detail TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL, updated TEXT NOT NULL)""")


def _line(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def compose_mail(receipt: Receipt, profile: Profile, reference: str) -> tuple[str, str]:
    """Fixed template with the ticket and profile data. No model writes any part of it."""
    number = _line(receipt.ticket_number or ', '.join(receipt.billing_codes), 120)
    merchant = _line(receipt.merchant, 120)
    subject = _line(f'Solicitud de factura {reference} · {merchant} · folio {number}', 240)
    body = (f'Buen día.\n\nSolicito la factura de mi compra en {merchant} del {receipt.date}, '
            f'folio {number}, por ${receipt.amount} MXN. Adjunto el ticket.\n\n'
            f'Receptor: {_line(profile.name, 254)}\nRFC: {profile.rfc}\nCódigo postal: {profile.postal_code}\n'
            f'Régimen fiscal: {profile.regime}\nUso de CFDI: {profile.cfdi_use}\nForma de pago: {receipt.payment_code}\n\n'
            f'Agradeceré que envíen el PDF y el XML a {profile.email} o como respuesta a este mensaje.\n'
            f'Referencia {reference}.\n\nGracias.')
    return subject, body


def smtp_security(account: dict) -> str:
    chosen = account.get('smtp_tls') or ''
    if chosen in {'ssl', 'starttls'}:
        return chosen
    return 'starttls' if int(account.get('smtp_port') or 465) in {25, 587, 2525} else 'ssl'


class SmtpSender:
    """SMTP with implicit TLS or mandatory STARTTLS (587 by default). Never plain text."""
    channel = 'smtp'
    refused = buzon.LOGIN

    def __init__(self, account: dict, password: str):
        self.account = dict(account)
        self.__password = password
        self.address = self.account.get('address') or self.account.get('user', '')
        self.smtp = None

    def __repr__(self):
        return f'SmtpSender({self.account.get("smtp_host", "")!r})'

    def _login(self, smtp):
        smtp.login(self.account['user'], self.__password)

    def connect(self):
        host, port = self.account['smtp_host'], int(self.account.get('smtp_port') or 465)
        try:
            if smtp_security(self.account) == 'ssl':
                smtp = smtplib.SMTP_SSL(host, port, context=buzon.tls_context(), timeout=30)
            else:
                smtp = smtplib.SMTP(host, port, timeout=30)
                smtp.ehlo()
                if not smtp.has_extn('starttls'):
                    smtp.close()
                    raise ValueError('El servidor de salida no ofrece conexión cifrada. Usa el puerto 465 o 587 con TLS.')
                smtp.starttls(context=buzon.tls_context())
                smtp.ehlo()
        except ssl.SSLCertVerificationError:
            raise ValueError(buzon.CERTIFICATE) from None
        except (OSError, smtplib.SMTPException):
            raise ValueError(buzon.CONNECTION) from None
        self.smtp = smtp
        try:
            self._login(smtp)
        except (smtplib.SMTPException, UnicodeError):
            self.close()
            raise buzon.LoginRefused(self.refused) from None
        except OSError:
            self.close()
            raise ValueError(buzon.CONNECTION) from None

    def prepare(self, recipient: str):
        """Open the session up to the recipient. No part of the message has left yet."""
        self.connect()
        try:
            code, _ = self.smtp.mail(self.address)
            if code != 250:
                raise ValueError(SENDER_REFUSED)
            code, _ = self.smtp.rcpt(recipient)
            if code not in (250, 251):
                raise ValueError(REFUSED)
        except (OSError, smtplib.SMTPException):
            raise ValueError(buzon.CONNECTION) from None

    def deliver(self, data: bytes) -> str:
        self.smtp.data(data)
        return ''

    def abort(self):
        try:
            self.smtp.rset()
        except Exception:
            pass

    def close(self):
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except Exception:
                try:
                    self.smtp.close()
                except Exception:
                    pass
            self.smtp = None


class OutlookSender(SmtpSender):
    """Outlook.com, Hotmail and Microsoft 365: the same SMTP, opened with a Microsoft access token."""
    channel = 'outlook'
    refused = OUTLOOK_SENDING

    def __init__(self, account: dict, tokens):
        super().__init__(account, '')
        self.__tokens = tokens   # tokens(force=False) -> an access token that is valid now
        self.__token = ''

    def __repr__(self):
        return 'OutlookSender()'

    def _login(self, smtp):
        try:
            smtp.ehlo_or_helo_if_needed()
            # The credentials do not fit in the AUTH line (RFC 4954): they go after the server's 334.
            smtp.auth('XOAUTH2', outlook.sasl(self.account['user'], self.__token), initial_response_ok=False)
        except smtplib.SMTPException:
            raise
        except ValueError:
            raise smtplib.SMTPException('XOAUTH2') from None  # A challenge that is not base64.

    def connect(self):
        self.__token = self.__tokens()
        try:
            try:
                super().connect()
            except buzon.LoginRefused:
                # A permission taken away stops a token before its time: renew it once and try again.
                fresh = self.__tokens(force=True)
                if not fresh or fresh == self.__token:
                    raise
                self.__token = fresh
                super().connect()
        finally:
            self.__token = ''


class GmailSender:
    """Gmail API send, available only when the optional send permission was granted."""
    channel = 'gmail'
    address = ''

    def __init__(self, root):
        self.root = Path(root)

    def prepare(self, recipient: str):
        if not gmail.can_send(self.root):
            raise ValueError(NO_SENDER)
        gmail.access_token(self.root)

    def deliver(self, data: bytes) -> str:
        return gmail.send_raw(self.root, data)

    def abort(self):
        pass

    def close(self):
        pass


def default_sender(root):
    account = buzon.load_account(root)
    if account and account.get('smtp_host'):
        return SmtpSender(account, buzon.account_password(root))
    if gmail.can_send(Path(root)):
        return GmailSender(root)
    microsoft = outlook.mail_account(root) if outlook.can_send(root) else None
    if microsoft:
        return OutlookSender(microsoft, lambda force=False: outlook.access_token(root, force=force))
    raise ValueError(NO_SENDER)


def can_send(root) -> bool:
    account = buzon.load_account(root)
    return bool(account and account.get('smtp_host')) or gmail.can_send(Path(root)) or outlook.can_send(root)


class MailOutbox:
    def __init__(self, store, senders=None):
        self.store = store
        self.root = store.root
        self.senders = senders or (lambda: default_sender(self.root))
        with store.connect() as db:
            create_tables(db)
            # Nothing left the program in 'preparing'. 'sending' may have reached the merchant.
            db.execute("DELETE FROM mail_requests WHERE state='preparing'")
            db.execute("UPDATE mail_requests SET state='uncertain',detail=?,updated=? WHERE state='sending'",
                       ('Se interrumpió el envío. ' + UNCERTAIN, now()))

    def status(self, key: str) -> dict:
        job = self.store.get(key)
        with self.store.connect() as db:
            req = db.execute('SELECT * FROM mail_requests WHERE job_id=?', (key,)).fetchone()
        common = {'permiso': permitido(self.root, 'pedir_por_correo'), 'puede_enviar': can_send(self.root)}
        if req is None:
            return {'status': 'new', 'to': '', 'reference': '', 'message': '', 'updated': '', **common}
        verified = job['status'] == 'verified'
        state = 'sending' if req['state'] == 'preparing' else req['state']
        return {'status': 'verified' if verified else state, 'to': req['address'], 'reference': req['reference'],
                'message': 'PDF y XML cotejados.' if verified else req['detail'], 'updated': req['updated'], **common}

    def _reserve(self, key, address):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self.store.decode(db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            if db.execute('SELECT 1 FROM mail_requests WHERE job_id=?', (key,)).fetchone():
                raise ValueError('Este expediente ya tiene una solicitud por correo. Su respuesta se busca sola en tu buzón.')
            receipt = Receipt(**job['receipt'])
            if (job['status'] not in {'ready', 'external'} or job['historical'] or job['uuid'] or
                    job['issue_attempted'] or job['human_exposed'] or job['issuance_seen'] or
                    receipt.source.startswith('Ejemplo:') or receipt.ready_errors()):
                raise ValueError('Solo puedes pedir por correo un ticket real revisado sin un intento previo de emisión.')
            reference = 'FACTURAI-' + secrets.token_hex(8).upper()
            db.execute('INSERT INTO mail_requests(job_id,address,reference,channel,state,detail,created,updated) '
                       'VALUES(?,?,?,?,?,?,?,?)', (key, address, reference, '', 'preparing', 'Preparando la solicitud.', now(), now()))
            return reference

    def _release(self, key):
        with self.store.connect() as db:
            db.execute("DELETE FROM mail_requests WHERE job_id=? AND state='preparing'", (key,))

    def _arm(self, key, profile, channel, message_id):
        # Committed before the message content is transmitted: an uncertain result is never resent.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("UPDATE mail_requests SET state='sending',channel=?,message_id=?,detail=?,updated=? "
                          "WHERE job_id=? AND state='preparing'",
                          (channel, message_id, 'Envío en curso.', now(), key)).rowcount != 1:
                raise ValueError('La solicitud cambió. Revisa el expediente.')
            if db.execute("""UPDATE jobs SET issue_attempted=1,status='external',billing_profile=?,message=?,updated=?
                       WHERE id=? AND status IN ('ready','external') AND historical=0 AND issue_attempted=0
                       AND human_exposed=0 AND issuance_seen=0 AND uuid IS NULL""",
                          (profile.model_dump_json(), 'Envío por correo iniciado.', now(), key)).rowcount != 1:
                raise ValueError('El expediente cambió antes de enviar.')
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)',
                       (key, now(), 'mail_attempt', 'Solicitud por correo al comercio'))

    def _finish(self, key, success):
        state = 'sent' if success else 'uncertain'
        detail = 'Solicitud enviada por correo. Esperando el PDF y el XML.' if success else UNCERTAIN
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE mail_requests SET state=?,detail=?,updated=? WHERE job_id=? AND state='sending'",
                       (state, detail, now(), key))
            db.execute("UPDATE jobs SET message=?,updated=? WHERE id=? AND status='external'", (detail, now(), key))
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)', (key, now(), 'mail_' + state, detail))

    def _ticket(self, row, reference):
        source = Path(row['source_path'] or '').resolve()
        if (not source.is_relative_to(self.store.root / 'originales') or not source.is_file()
                or source.stat().st_size > MAX_UPLOAD):
            raise ValueError('El archivo original del ticket no está disponible.')
        data = source.read_bytes()
        mime = validate_document(source.name, data)
        return f'ticket-{reference}{source.suffix.lower()}', mime, data

    def request(self, key: str, to: str, consent: bool, profile: Profile) -> dict:
        """Send the request once. Only with the person's consent for this ticket or the standing permission."""
        address = buzon.valid_address(to)
        if consent is not True and not permitido(self.root, 'pedir_por_correo'):
            raise ValueError('Confirma que quieres pedir la factura por correo a esa dirección.')
        if not SEND_LOCK.acquire(blocking=False):
            raise ValueError('Hay un envío de correo en curso.')
        try:
            reference = self._reserve(key, address)
            sender = None
            try:
                row = self.store.get(key)
                receipt = Receipt(**row['receipt'])
                name, mime, data = self._ticket(row, reference)
                sender = self.senders()
                subject, body = compose_mail(receipt, profile, reference)
                message = EmailMessage()
                if sender.address:
                    message['From'] = sender.address
                message['To'] = address
                message['Subject'] = subject
                message['Date'] = formatdate(localtime=True)
                domain = (sender.address.rpartition('@')[2] or 'facturai.invalid') if sender.address else 'facturai.invalid'
                message['Message-ID'] = make_msgid('facturai', domain=domain)
                message.set_content(body)
                maintype, _, subtype = (mime or 'application/octet-stream').partition('/')
                message.add_attachment(data, maintype=maintype, subtype=subtype or 'octet-stream', filename=name)
                payload = message.as_bytes(policy=policy.SMTP)
                sender.prepare(address)
            except BaseException as exc:
                if sender is not None:
                    sender.close()
                self._release(key)
                if isinstance(exc, ValueError):
                    raise
                if isinstance(exc, Exception):
                    raise ValueError('No se inició el envío. Revisa la cuenta de correo y el ticket.') from None
                raise
            try:
                self._arm(key, profile, sender.channel, str(message['Message-ID']))
            except BaseException:
                sender.abort()
                sender.close()
                self._release(key)
                raise
            try:
                sender.deliver(payload)
            except Exception:
                self._finish(key, False)
                return self.status(key)
            finally:
                sender.close()
            self._finish(key, True)
            return self.status(key)
        finally:
            SEND_LOCK.release()
