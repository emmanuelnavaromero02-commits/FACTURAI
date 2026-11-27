"""Registration email for the HTTPS portal currently under control, from any connected mailbox."""
import asyncio
import ctypes
import ctypes.util
import re
import secrets
import time
from email.utils import getaddresses
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from . import buzon
from .catalog import public_portal_origin
from .gmail import parse_eml, read_selected_message, search_tickets
from .portal_accounts import REGISTRATION_PAGE, fill_portal_temporary


@lru_cache(maxsize=1)
def suffix_library():
    library = ctypes.util.find_library("psl")
    if not library:
        return None
    lib = ctypes.CDLL(library)
    lib.psl_builtin.restype = ctypes.c_void_p
    lib.psl_registrable_domain.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.psl_registrable_domain.restype = ctypes.c_char_p
    return lib


def mail_domain(host):
    lib = suffix_library()
    if lib:
        root = lib.psl_registrable_domain(lib.psl_builtin(), host.encode("ascii"))
        if root:
            return root.decode("ascii")
    return host


def matches_sender(sender, host):
    domains = [addr.rpartition("@")[2].lower() for _, addr in getaddresses([str(sender)])]
    if len(domains) != 1 or not re.fullmatch(r"[a-z0-9.-]+", domains[0]):
        return False
    return mail_domain(domains[0]) == mail_domain(host)


class MailHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.anchor = None
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        elif not self.hidden and tag == "a":
            self.anchor = [dict(attrs).get("href") or "", []]
        elif tag in ("p", "br", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)
        elif tag == "a" and self.anchor:
            self.links.append((self.anchor[0], " ".join(self.anchor[1])))
            self.anchor = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.anchor:
                self.anchor[1].append(data)


ACTIVATE = re.compile(r"activat|activa|verif|confirm|validaci[oó]n|completa.{0,12}registro", re.I)
TEMPORARY = re.compile(r"(?im)\b(?:contrase[ñn]a\s+(?:temporal|provisional)|clave\s+(?:temporal|provisional)|temporary\s+password)\s*(?::|es\s*:?)\s*([^\s<>]{4,128})")
REGISTRATION_MAIL = re.compile(r"activa(?:r|ci[oó]n)? (?:de |tu |su |la )*(?:cuenta|registro)|"
                               r"(?:confirma(?:ci[oó]n)?|completa|verifica)\s+(?:de |tu |su |el )*(?:registro|correo)|"
                               r"registro (?:de (?:la |tu )?)?(?:cuenta|usuario)|verify your email|account activation", re.I)
ACCESS_MAIL = re.compile(r"restablec|recupera.{0,20}contrase|password reset|segundo factor|dos pasos|doble factor|"
                         r"two.factor|multi.factor|authenticator|inicio de sesi[oó]n|sign.in (?:code|link)|"
                         r"c[oó]digo (?:de )?(?:acceso|seguridad)", re.I)
ENROLLMENT_CODE = re.compile(r"\bc[oó]digo (?:de (?:activaci[oó]n|registro|confirmaci[oó]n|verificaci[oó]n)|"
                             r"para (?:activar|confirmar|verificar) (?:tu |el )?(?:cuenta|registro|correo))"
                             r"\s*(?::|es\s*:?)\s*(\d{4,10})\b", re.I)


def registration_actions(message, origin, recipient, *, from_automatic_search=False):
    host = urlsplit(origin).hostname
    if not host or public_portal_origin(origin) != origin or not matches_sender(message.get("From", ""), host):
        raise ValueError("El remitente no corresponde al dominio del portal. Revisa el correo en tu buzón.")
    delivered = getaddresses([str(v) for k in ("To", "Cc", "Delivered-To", "X-Original-To") for v in message.get_all(k, [])])
    if recipient.casefold() not in {addr.casefold() for _, addr in delivered}:
        raise ValueError("El correo seleccionado tiene otro destinatario.")
    text, links = [], []
    for kind in ("plain", "html"):
        part = message.get_body(preferencelist=(kind,))
        if not part:
            continue
        try:
            body = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if not isinstance(body, str) or len(body) > 1_000_000:
            continue
        if kind == "html":
            parser = MailHTML()
            parser.feed(body)
            links.extend(parser.links)
            text.append("".join(parser.parts))
        else:
            text.append(body)
            links.extend((m.group(0).rstrip(".,)>"), "") for m in re.finditer(r"https://[^\s<>\"']+", body))
    candidates = {u for u,label in links if public_portal_origin(u) == origin and ACTIVATE.search(label + " " + urlsplit(u).path)}
    passwords = {v for body in text for v in TEMPORARY.findall(body)}
    actions = {}
    if len(candidates) == 1:
        actions["activate"] = candidates.pop()
    if len(passwords) == 1:
        actions["temporary"] = passwords.pop()
    evidence = "\n".join([str(message.get("Subject", "")), *text])
    if REGISTRATION_MAIL.search(evidence) and not ACCESS_MAIL.search(evidence):
        codes = set(ENROLLMENT_CODE.findall(evidence))
        if len(codes) == 1:
            actions["code"] = codes.pop()
    elif from_automatic_search:
        raise ValueError("El mensaje no identifica una activación de registro.")
    if not actions:
        raise ValueError("No se identificó una única clave temporal ni una liga de activación de este portal. Revisa el mensaje en tu buzón.")
    return actions


def gmail_authenticated(message, host):
    """Gmail's own inbound aligned DMARC result, exactly as the Gmail-only flow requires it."""
    sender = str(message.get("From", ""))
    if not matches_sender(sender, host):
        return False
    reports = message.get_all("Authentication-Results", [])
    if not reports:
        return False
    authority, _, results = str(reports[0]).partition(";")
    if authority.strip().casefold() != buzon.GMAIL_AUTHORITY:
        return False
    result = re.search(r"(?:^|;)\s*dmarc=pass\b[^;]*?\bheader\.from=([a-z0-9.-]+)(?:\s|;|$)", results, re.I)
    from_host = getaddresses([sender])[0][1].rpartition("@")[2].casefold()
    return bool(result and result.group(1).lower() == from_host)


def authenticated_by(message, host, authority):
    """Require the mailbox provider's own Authentication-Results with DKIM or DMARC passed and aligned.

    The provider writes its header on top. `authority` is its authserv-id, pinned when the
    account was connected; without it nothing is accepted automatically.
    """
    sender = str(message.get("From", ""))
    if not authority or not matches_sender(sender, host):
        return False
    reports = message.get_all("Authentication-Results", [])
    if not reports or buzon.authserv_id(reports[0]) != authority.casefold():
        return False
    from_host = getaddresses([sender])[0][1].rpartition("@")[2].casefold()
    results = re.sub(r"\([^)]*\)", " ", str(reports[0]).partition(";")[2])
    for item in results.split(";"):
        found = re.match(r"\s*(dmarc|dkim)\s*=\s*pass\b(.*)", item, re.I | re.S)
        if not found:
            continue
        method, properties = found.group(1).lower(), found.group(2)
        if method == "dmarc":
            header = re.search(r"\bheader\.from\s*=\s*([a-z0-9.-]+)", properties, re.I)
            if header and header.group(1).lower().rstrip(".") == from_host:
                return True
        else:
            domain = re.search(r"\bheader\.d\s*=\s*([a-z0-9.-]+)", properties, re.I)
            if domain and mail_domain(domain.group(1).lower().rstrip(".")) == mail_domain(from_host):
                return True
    return False


def registration_email(root, origin, recipient, started, kind, *, search=None, read=None, authenticated=None,
                       mailboxes=None):
    """Look only at messages received after this specific enrollment began, in every connected mailbox.

    Gmail keeps the same query and the same DMARC rule as before (search, read and
    authenticated let the caller keep its own Gmail helpers). IMAP mailboxes need the
    provider's pinned authserv-id; a message that cannot be verified is left for the person.
    """
    now = time.time()
    if not 0 <= now - started <= 30 * 60:
        raise ValueError("La solicitud de alta venció. Revisa el correo del comercio en el navegador.")
    if not re.fullmatch(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+", recipient):
        raise ValueError("Abre el correo del comercio en el navegador para verificar este destinatario.")
    host = urlsplit(origin).hostname
    others = mailboxes() if mailboxes else buzon.imap_mailboxes(root)
    matches = []
    if buzon.gmail_connected(root) or not others:
        search, read = search or search_tickets, read or read_selected_message
        authenticated = authenticated or gmail_authenticated
        query = f"to:{recipient} from:({mail_domain(host)}) after:{int(started)} -in:spam -in:trash"
        for row in search(root, query):
            received = row["received_ms"] / 1000
            if not started <= received <= now + 1 or not matches_sender(row["sender"], host):
                continue
            message = read(root, row["id"])
            if not authenticated(message, host):
                continue
            try:
                actions = registration_actions(message, origin, recipient, from_automatic_search=True)
            except ValueError:
                continue
            if kind in actions:
                matches.append(actions[kind])
    for box in others:
        if not box.authority:
            continue
        with box:
            for row in box.search(since=started, sender=mail_domain(host), recipient=recipient, limit=10):
                if not started <= row["received"] <= now + 1 or not matches_sender(row["sender"], host):
                    continue
                message = box.read(row["ref"])
                if message is None or not authenticated_by(message, host, box.authority):
                    continue
                try:
                    actions = registration_actions(message, origin, recipient, from_automatic_search=True)
                except ValueError:
                    continue
                if kind in actions:
                    matches.append(actions[kind])
    if len(matches) > 1:
        raise ValueError("Hay varios mensajes de alta que coinciden. Elige el correo correcto en el portal.")
    return matches[0] if matches else None


class MailContext(BaseModel):
    origin: str = Field(max_length=255)
    channel: str = Field(min_length=1, max_length=80)
    revision: int


class MailChoice(MailContext):
    choice: str = Field(min_length=1, max_length=220)


def portal_mail_router(root, viewer, profile):
    router = APIRouter(prefix="/api/portal-mail")
    # Only the last search or selected message in this personal viewer may be used.
    pending = {}
    eviction = None

    def check(req):
        viewer.require_control(req.channel)
        if (viewer.revision != req.revision or public_portal_origin(req.origin) != req.origin
                or public_portal_origin(viewer.page.url) != req.origin):
            raise ValueError("La página cambió. Revisa nuevamente el dominio del comercio.")
        return profile().email

    def bind(req, values):
        nonlocal eviction
        email = check(req)
        if eviction is not None:
            eviction.cancel()
        pending.clear()
        pending.update(context=(req.channel, req.revision, req.origin, email), deadline=time.monotonic()+300, values=values)
        eviction = asyncio.get_running_loop().call_later(300, pending.clear)

    def selected(req, prefix):
        email = check(req)
        if (pending.get("context") != (req.channel, req.revision, req.origin, email)
                or pending.get("deadline", 0) <= time.monotonic()):
            pending.clear()
            raise ValueError("La selección de correo venció. Vuelve a elegir el mensaje.")
        key = prefix + req.choice
        if key not in pending["values"]:
            raise ValueError("La selección no corresponde al correo de este portal.")
        return pending["values"][key]

    def prepare(req, message):
        choices = registration_actions(message, req.origin, check(req))
        bind(req, {"action:" + k: v for k, v in choices.items()})
        return {"actions": list(choices)}

    @router.post("/search")
    async def search(req: MailContext):
        email = check(req)
        domain = mail_domain(urlsplit(req.origin).hostname)
        if not re.fullmatch(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+", email):
            raise ValueError("Para este formato de correo usa la opción de archivo .eml.")
        rows = await asyncio.to_thread(buzon.search_all, root, since=time.time() - 7 * 86400, sender=domain,
                                       recipient=email, limit=10)
        choices, output = {}, []
        for row in rows:
            if matches_sender(row["sender"], urlsplit(req.origin).hostname):
                key = secrets.token_urlsafe(14)
                choices["message:" + key] = row["ref"]
                output.append({"choice": key, "sender": row["sender"], "subject": row["subject"], "date": row["date"]})
        bind(req, choices)
        return {"messages": output}

    @router.post("/select")
    async def select(req: MailChoice):
        ref = selected(req, "message:")
        message = await asyncio.to_thread(buzon.read_ref, root, ref)
        if message is None:
            raise ValueError("El correo supera el límite de tamaño.")
        return prepare(req, message)

    @router.post("/file")
    async def load_file(origin: str = Form(max_length=255), channel: str = Form(max_length=80), revision: int = Form(),
                        file: UploadFile = File()):
        req = MailContext(origin=origin, channel=channel, revision=revision)
        check(req)
        data = await file.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("El correo de registro debe pesar menos de 2 MB.")
        return prepare(req, parse_eml(data))

    @router.post("/apply")
    async def apply(req: MailChoice):
        value = selected(req, "action:")
        pending["values"].pop("action:" + req.choice)
        if req.choice == "temporary":
            await fill_portal_temporary(viewer, req.channel, req.origin, value)
        elif req.choice == "code":
            async with viewer.commands:
                check(req)
                result = await viewer.page.evaluate(REGISTRATION_PAGE, {
                    'origin': req.origin, 'operation': 'fill_code', 'value': value})
                if result != 'filled':
                    raise ValueError('El formulario no muestra un campo inequívoco de activación del registro.')
        elif req.choice == "activate":
            async with viewer.commands:
                check(req)
                if public_portal_origin(value) != req.origin:
                    raise ValueError("La liga no pertenece al portal confirmado.")
                try:
                    await viewer.page.goto(value, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    raise ValueError("El portal no confirmó la apertura de la liga. Revisa la página.") from None
        return {"done": True}

    return router
