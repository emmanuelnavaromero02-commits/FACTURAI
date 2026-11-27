"""In-process IMAP and SMTP servers over TLS for the mail tests. No real network is used."""
from __future__ import annotations

import base64
import datetime
import email
import re
import socket
import socketserver
import ssl
import tempfile
import threading
from email import policy
from email.message import EmailMessage
from ipaddress import IPv4Address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_CERTS = None


def certificates():
    """A private test CA and a server certificate for localhost / 127.0.0.1 (created once)."""
    global _CERTS
    if _CERTS:
        return _CERTS
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CA de prueba FACTURAI')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.IPAddress(IPv4Address('127.0.0.1'))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    folder = Path(tempfile.mkdtemp(prefix='correo-tls-'))
    chain = folder / 'server.pem'
    chain.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _CERTS = (ca.public_bytes(serialization.Encoding.PEM).decode('ascii'), str(chain))
    return _CERTS


def trusted_context():
    """Same verification as production (chain and host name), trusting only the test CA."""
    context = ssl.create_default_context(cadata=certificates()[0])
    return context


def server_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates()[1])
    return context


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # A client that rejects the certificate or drops the line is part of the tests.


class _Handler(socketserver.StreamRequestHandler):
    def finish(self):
        if getattr(self, 'wfile', None) is not None:
            try:
                super().finish()
            except (OSError, ValueError):
                pass
        if isinstance(self.request, ssl.SSLSocket):
            self.request.close()  # wrap_socket detached the socket the server would close.


class _Base:
    def __init__(self, handler):
        self.server = _Server(('127.0.0.1', 0), handler)
        self.server.owner = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _tokens(text):
    return [b if b else a for a, b in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', text)]


def xoauth2(payload):
    """User and access token of a SASL XOAUTH2 answer, or two empty texts when it is not one."""
    try:
        text = base64.b64decode(payload, validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        return '', ''
    found = re.fullmatch('user=([^\x01]*)\x01auth=Bearer ([^\x01]*)\x01\x01', text)
    return (found.group(1), found.group(2)) if found else ('', '')


class _ImapHandler(_Handler):
    def setup(self):
        try:
            self.request = server_context().wrap_socket(self.request, server_side=True)
        except (ssl.SSLError, OSError):
            self.request = None
            return
        super().setup()

    def handle(self):
        if self.request is None:
            return
        imap = self.server.owner
        self.out(b'* OK IMAP4rev1 de prueba listo\r\n')
        while True:
            try:
                line = self.rfile.readline()
            except (OSError, ssl.SSLError):
                return
            if not line:
                return
            text = line.decode('utf-8', 'replace').rstrip('\r\n')
            tag, _, rest = text.partition(' ')
            command, _, args = rest.partition(' ')
            command = command.upper()
            if command == 'UID':
                command, _, args = args.partition(' ')
                command = 'UID ' + command.upper()
            imap.commands.append(command)
            if command == 'CAPABILITY':
                extra = b' AUTH=XOAUTH2' if imap.tokens is not None else b''
                self.out(b'* CAPABILITY IMAP4rev1 AUTH=PLAIN' + extra + b'\r\n' + tag.encode() + b' OK listo\r\n')
            elif command == 'AUTHENTICATE' and imap.tokens is not None:
                mechanism, _, answer = args.partition(' ')
                if mechanism.upper() != 'XOAUTH2':
                    self.out(tag.encode() + b' NO mecanismo no admitido\r\n')
                    continue
                if not answer:
                    self.out(b'+ \r\n')
                    answer = self.rfile.readline().decode('ascii', 'replace').strip()
                user, token = xoauth2(answer)
                imap.presented.append((user, token))
                if token and imap.tokens.get(token) == user:
                    imap.logins += 1
                    self.out(tag.encode() + b' OK AUTHENTICATE completed.\r\n')
                else:
                    self.out(tag.encode() + b' NO AUTHENTICATE failed.\r\n')
            elif command == 'LOGIN':
                parts = re.fullmatch(r'(\S+) "((?:[^"\\]|\\.)*)"', args)
                user = parts.group(1) if parts else ''
                password = re.sub(r'\\(.)', r'\1', parts.group(2)) if parts else ''
                # A server that takes tokens refuses every password, as Microsoft's do.
                if imap.tokens is None and user == imap.user and password == imap.password:
                    imap.logins += 1
                    self.out(tag.encode() + b' OK sesion iniciada\r\n')
                else:
                    self.out(tag.encode() + b' NO [AUTHENTICATIONFAILED] credenciales invalidas\r\n')
            elif command in {'EXAMINE', 'SELECT'}:
                imap.selected.append(command)
                self.out(f'* {len(imap.messages)} EXISTS\r\n* OK [UIDVALIDITY {imap.validity}] listo\r\n'
                         f'{tag} OK [READ-ONLY] completo\r\n'.encode())
            elif command == 'UID SEARCH':
                found = [str(m['uid']) for m in imap.messages if imap.matches(m, _tokens(args))]
                self.out(('* SEARCH ' + ' '.join(found)).rstrip().encode() + b'\r\n' + tag.encode() + b' OK busqueda\r\n')
            elif command == 'UID FETCH':
                uid, _, items = args.partition(' ')
                self.fetch(tag, int(uid), items)
            elif command == 'LOGOUT':
                self.out(b'* BYE adios\r\n' + tag.encode() + b' OK adios\r\n')
                return
            elif command == 'NOOP':
                self.out(tag.encode() + b' OK\r\n')
            else:
                self.out(tag.encode() + b' BAD comando no admitido\r\n')

    def out(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def fetch(self, tag, uid, items):
        imap = self.server.owner
        message = next((m for m in imap.messages if m['uid'] == uid), None)
        if message is None:
            self.out(tag.encode() + b' OK nada\r\n')
            return
        seq = imap.messages.index(message) + 1
        pieces = [f'UID {uid}'.encode()]
        literal = None
        upper = items.upper()
        if 'INTERNALDATE' in upper:
            pieces.append(b'INTERNALDATE "' + message['date'].strftime('%d-%b-%Y %H:%M:%S +0000').encode() + b'"')
        if 'RFC822.SIZE' in upper:
            pieces.append(b'RFC822.SIZE ' + str(len(message['raw'])).encode())
        fields = re.search(r'BODY\.PEEK\[HEADER\.FIELDS \(([^)]*)\)\]', items, re.I)
        if fields:
            wanted = {f.lower() for f in fields.group(1).split()}
            head = message['raw'].split(b'\r\n\r\n', 1)[0].split(b'\r\n')
            kept, keep = [], False
            for line in head:
                if line[:1] in (b' ', b'\t'):
                    if keep:
                        kept.append(line)
                    continue
                keep = line.split(b':', 1)[0].strip().lower().decode() in wanted
                if keep:
                    kept.append(line)
            literal = (b'BODY[HEADER.FIELDS (' + fields.group(1).encode() + b')]', b'\r\n'.join(kept) + b'\r\n\r\n')
        elif re.search(r'BODY\.PEEK\[\]', items, re.I):
            imap.downloads.append(uid)
            literal = (b'BODY[]', message['raw'])
        data = b'* ' + str(seq).encode() + b' FETCH (' + b' '.join(pieces)
        if literal:
            data += b' ' + literal[0] + b' {' + str(len(literal[1])).encode() + b'}\r\n' + literal[1]
        self.out(data + b')\r\n' + tag.encode() + b' OK fetch\r\n')


class FakeImap(_Base):
    """IMAP4rev1 subset that imaplib uses: LOGIN, EXAMINE, UID SEARCH and UID FETCH.

    With ``tokens`` (access token -> mailbox address) it also takes AUTHENTICATE XOAUTH2, the way
    Microsoft's servers do: a token opens only the mailbox it was issued for.
    """

    def __init__(self, user='buzon@example.org', password='clave-app-ficticia', messages=(), tokens=None):
        self.user, self.password = user, password
        self.tokens, self.presented = tokens, []
        self.messages, self.commands, self.selected, self.downloads = [], [], [], []
        self.logins, self.validity = 0, 7
        for raw, when in messages:
            self.add(raw, when)
        super().__init__(_ImapHandler)

    def add(self, raw, when):
        if isinstance(raw, EmailMessage):
            raw = raw.as_bytes(policy=policy.SMTP)
        self.messages.append({'uid': len(self.messages) + 101, 'raw': raw, 'date': when})

    def matches(self, message, tokens):
        head = email.message_from_bytes(message['raw'], policy=policy.default)
        index = 0
        while index < len(tokens):
            key = tokens[index].upper()
            if key == 'ALL':
                index += 1
                continue
            value = tokens[index + 1]
            index += 2
            if key == 'SINCE':
                if message['date'].date() < datetime.datetime.strptime(value, '%d-%b-%Y').date():
                    return False
            elif key == 'FROM':
                if value.lower() not in str(head.get('From', '')).lower():
                    return False
            elif key == 'TO':
                if value.lower() not in str(head.get('To', '')).lower():
                    return False
            elif key == 'TEXT':
                if value.lower().encode() not in message['raw'].lower():
                    return False
            else:
                return False
        return True

    def account(self, **changes):
        return {'imap_host': '127.0.0.1', 'imap_port': self.port, 'user': self.user, 'address': self.user,
                'smtp_host': '', 'smtp_port': 465, 'authority': '', **changes}


class _SmtpHandler(_Handler):
    def setup(self):
        smtp = self.server.owner
        self.secure = False
        if smtp.mode == 'ssl':
            try:
                self.request = server_context().wrap_socket(self.request, server_side=True)
            except (ssl.SSLError, OSError):
                self.request = None
                return
            self.secure = True
        super().setup()

    def out(self, line):
        self.wfile.write(line.encode() + b'\r\n')
        self.wfile.flush()

    def handle(self):
        if self.request is None:
            return
        smtp = self.server.owner
        self.out('220 smtp de prueba')
        envelope = {}
        authenticated = False
        while True:
            try:
                line = self.rfile.readline()
            except (OSError, ssl.SSLError):
                return
            if not line:
                return
            text = line.decode('utf-8', 'replace').rstrip('\r\n')
            verb = text.split(' ', 1)[0].upper()
            smtp.commands.append(verb)
            if verb in {'EHLO', 'HELO'}:
                features = ['250-smtp de prueba']
                if smtp.mode == 'starttls' and not self.secure:
                    features.append('250-STARTTLS')
                if self.secure:
                    features.append('250-AUTH PLAIN' + (' XOAUTH2' if smtp.tokens is not None else ''))
                features.append('250 OK')
                for item in features:
                    self.out(item)
            elif verb == 'STARTTLS' and smtp.mode == 'starttls':
                self.out('220 listo para TLS')
                try:
                    self.request = server_context().wrap_socket(self.request, server_side=True)
                except (ssl.SSLError, OSError):
                    return
                self.secure = True
                self.rfile = self.request.makefile('rb')
                self.wfile = self.request.makefile('wb')
            elif verb == 'AUTH':
                if not self.secure:
                    self.out('538 se requiere cifrado')
                    continue
                parts = text.split(' ')
                if smtp.tokens is not None and len(parts) > 1 and parts[1].upper() == 'XOAUTH2':
                    inline = len(parts) > 2
                    if not inline:
                        self.out('334 ')
                        parts.append(self.rfile.readline().decode('ascii', 'replace').strip())
                    user, token = xoauth2(parts[2])
                    smtp.presented.append((user, token, inline))
                    if token and smtp.tokens.get(token) == user:
                        authenticated = True
                        smtp.logins += 1
                        self.out('235 2.7.0 Authentication successful')
                    else:
                        self.out('535 5.7.3 Authentication unsuccessful')
                    continue
                if smtp.tokens is not None:
                    self.out('535 5.7.139 Authentication unsuccessful, basic authentication is disabled')
                    continue
                try:
                    _, user, password = base64.b64decode(parts[2]).decode().split('\0')
                except (IndexError, ValueError):
                    self.out('501 sintaxis')
                    continue
                if user == smtp.user and password == smtp.password:
                    authenticated = True
                    smtp.logins += 1
                    self.out('235 aceptado')
                else:
                    self.out('535 credenciales invalidas')
            elif verb == 'MAIL':
                if not authenticated:
                    self.out('530 autenticacion requerida')
                    continue
                envelope = {'from': re.search(r'<([^>]*)>', text).group(1), 'to': []}
                self.out('250 OK')
            elif verb == 'RCPT':
                address = re.search(r'<([^>]*)>', text).group(1)
                if address in smtp.refuse:
                    self.out('550 buzon inexistente')
                    continue
                envelope['to'].append(address)
                self.out('250 OK')
            elif verb == 'DATA':
                if smtp.on_data:
                    smtp.on_data()
                self.out('354 termina con punto')
                lines = []
                while True:
                    chunk = self.rfile.readline()
                    if not chunk or chunk == b'.\r\n':
                        break
                    lines.append(chunk[1:] if chunk.startswith(b'..') else chunk)
                if smtp.drop_after_data:
                    smtp.received.append({**envelope, 'data': b''.join(lines)})
                    self.request.shutdown(socket.SHUT_RDWR)
                    return
                smtp.received.append({**envelope, 'data': b''.join(lines)})
                self.out('250 entregado')
            elif verb == 'RSET':
                envelope = {}
                self.out('250 OK')
            elif verb == 'QUIT':
                self.out('221 adios')
                return
            else:
                self.out('500 no admitido')


class FakeSmtp(_Base):
    """SMTP over TLS with AUTH PLAIN and, with ``tokens`` (access token -> address), AUTH XOAUTH2."""

    def __init__(self, mode='ssl', user='buzon@example.org', password='clave-app-ficticia', tokens=None):
        self.mode, self.user, self.password = mode, user, password
        self.tokens, self.presented = tokens, []
        self.commands, self.received, self.refuse = [], [], set()
        self.logins = 0
        self.on_data = None
        self.drop_after_data = False
        super().__init__(_SmtpHandler)


def parsed(item):
    return email.message_from_bytes(item['data'], policy=policy.default)
