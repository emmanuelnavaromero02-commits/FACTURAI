from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from .catalog import CUSTOM_PORTAL, public_portal_origin


# SAT Anexo 20, catCFDI.xsd/c_FormaPago. Keep the receipt, portal selector and
# review form on the same set of two-character codes.
SAT_PAYMENT_LABELS = {
    '01': 'Efectivo', '02': 'Cheque nominativo', '03': 'Transferencia electrónica de fondos',
    '04': 'Tarjeta de crédito', '05': 'Monedero electrónico', '06': 'Dinero electrónico',
    '08': 'Vales de despensa', '12': 'Dación en pago', '13': 'Pago por subrogación',
    '14': 'Pago por consignación', '15': 'Condonación', '17': 'Compensación',
    '23': 'Novación', '24': 'Confusión', '25': 'Remisión de deuda',
    '26': 'Prescripción o caducidad', '27': 'A satisfacción del acreedor',
    '28': 'Tarjeta de débito', '29': 'Tarjeta de servicios',
    '30': 'Aplicación de anticipos', '31': 'Intermediario de pagos', '99': 'Por definir',
}
SAT_PAYMENT_CODES = frozenset(SAT_PAYMENT_LABELS)
SAT_PAYMENT_ALIASES = {code: (label,) for code, label in SAT_PAYMENT_LABELS.items()}
SAT_PAYMENT_ALIASES.update({
    '01': ('Efectivo',), '02': ('Cheque nominativo', 'Cheque'),
    '03': ('Transferencia electrónica de fondos', 'Transferencia'),
    '04': ('Tarjeta de crédito', 'Crédito'), '28': ('Tarjeta de débito', 'Débito'),
})


def money(value: str | int | Decimal) -> Decimal:
    if isinstance(value, float):
        raise ValueError("Usa texto decimal para importes; no float")
    try:
        result = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise ValueError("Importe inválido") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("El importe debe ser positivo y finito")
    if result.adjusted() > 10:
        raise ValueError("Importe fuera del rango de esta versión")
    rounded = result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if result != rounded:
        raise ValueError("El importe debe tener como máximo dos decimales")
    return rounded


class Profile(BaseModel):
    rfc: str
    name: str
    postal_code: str
    regime: str
    cfdi_use: str = "G03"
    email: str
    whatsapp_phone: str = Field(default='', max_length=32)

    @field_validator('whatsapp_phone')
    @classmethod
    def valid_whatsapp_phone(cls, value: str) -> str:
        number = re.sub(r'[\s().-]', '', value)
        if number and not re.fullmatch(r'\+[1-9]\d{7,14}', number):
            raise ValueError('Escribe tu propio número de WhatsApp con + y el código de país.')
        return number

    @field_validator("rfc")
    @classmethod
    def valid_rfc(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}", value):
            raise ValueError("RFC debe tener 12 o 13 caracteres y estructura válida")
        return value

    @field_validator("postal_code")
    @classmethod
    def valid_postal(cls, value: str) -> str:
        if not re.fullmatch(r"\d{5}", value):
            raise ValueError("Código postal de cinco dígitos")
        return value

    @field_validator("regime")
    @classmethod
    def valid_regime(cls, value: str) -> str:
        if not re.fullmatch(r"\d{3}", value):
            raise ValueError("Régimen: código SAT de tres dígitos")
        return value

    @field_validator("cfdi_use")
    @classmethod
    def valid_use(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{1,2}\d{2}", value):
            raise ValueError("Uso CFDI inválido")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Correo inválido")
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = " ".join(value.upper().split())
        if len(value) < 3:
            raise ValueError("Razón social o nombre incompleto")
        return value


class Receipt(BaseModel):
    provider: str = "unknown"
    portal_url: str = Field(default='', max_length=2048)
    merchant: str = ""
    amount: str = ""
    date: str = ""
    ticket_number: str = ""
    billing_codes: list[str] = Field(default_factory=list)
    payment_code: str = ""
    details: str = ""
    source: str = "archivo"
    # Merchant contact data printed on the ticket. Invalid values are kept empty.
    issuer_rfc: str = ''
    merchant_whatsapp: str = ''
    merchant_email: str = ''

    @field_validator('issuer_rfc', mode='before')
    @classmethod
    def valid_issuer_rfc(cls, value) -> str:
        try:
            return Profile.valid_rfc(value) if isinstance(value, str) and value.strip() else ''
        except ValueError:
            return ''

    @field_validator('merchant_whatsapp', mode='before')
    @classmethod
    def valid_merchant_whatsapp(cls, value) -> str:
        if not isinstance(value, str) or len(value) > 64:
            return ''
        number = re.sub(r'[\s().-]', '', value.strip().removeprefix('https://').removeprefix('wa.me/'))
        if number.startswith('00'):
            number = '+' + number[2:]
        if re.fullmatch(r'\d{10}', number):
            number = '+52' + number  # Ten digits are a Mexican national number.
        elif re.fullmatch(r'52\d{10}', number):
            number = '+' + number
        try:
            return Profile.valid_whatsapp_phone(number) if number else ''
        except ValueError:
            return ''

    @field_validator('merchant_email', mode='before')
    @classmethod
    def valid_merchant_email(cls, value) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 254:
            return ''
        try:
            return Profile.valid_email(value)
        except ValueError:
            return ''

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        if value:
            from datetime import date
            date.fromisoformat(value)
        return value

    @field_validator("payment_code")
    @classmethod
    def valid_payment(cls, value: str) -> str:
        if value and value not in SAT_PAYMENT_CODES:
            raise ValueError("Forma de pago fuera del catálogo SAT")
        return value

    def ready_errors(self) -> list[str]:
        result = []
        if self.provider == CUSTOM_PORTAL and not public_portal_origin(self.portal_url):
            result.append('Indica la dirección HTTPS pública del portal del comercio.')
        if self.provider != CUSTOM_PORTAL and self.portal_url:
            result.append('Selecciona el portal del ticket para usar una dirección personalizada.')
        try:
            money(self.amount)
        except ValueError as exc:
            result.append(str(exc))
        if not self.date:
            result.append("Falta fecha")
        if not (self.ticket_number or self.billing_codes):
            result.append("Falta número de ticket o código de facturación")
        if not self.payment_code:
            result.append("Falta forma de pago confirmada")
        elif self.payment_code == '99':
            result.append('La clave 99 no confirma cómo se pagó esta compra.')
        if not self.merchant:
            result.append("Falta comercio")
        return result

    def merchant_key(self):
        if self.provider == CUSTOM_PORTAL:
            return [self.provider, public_portal_origin(self.portal_url), self.merchant.strip().casefold()]
        return self.provider if self.provider != 'solicitud_directa' else [self.provider, self.merchant.strip().casefold()]

    def identity(self) -> str:
        # No normalizar O/0, I/1 ni eliminar guiones de un código comercial.
        keys = sorted(set(x.strip() for x in self.billing_codes if x.strip()))
        identity = [self.merchant_key(), keys or [self.ticket_number.strip()], self.date]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_receipt: bool
    provider: str
    merchant: str
    amount: str | None
    date: str | None
    ticket_number: str | None
    billing_codes: list[str]
    billing_url: str | None
    payment_code: str | None
    payment_evidence: str | None
    details: str
    uncertainties: list[str]
    # Required and nullable in the strict schema; the default only keeps older replies valid.
    issuer_rfc: str | None = None
    merchant_whatsapp: str | None = None
    merchant_email: str | None = None


class WebAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    answer: str


class PortalSearch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    billing_url: str | None
    evidence_url: str | None


class TaxProfileExtraction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    is_constancia: bool
    rfc: str | None
    name: str | None
    postal_code: str | None
    regime: str | None
    regimes: list[str]
    email: str | None
    uncertainties: list[str]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # field: name is the number «#n» from the «campos» list, for controls without a usable label.
    by: Literal["role", "label", "text", "placeholder", "field"]
    name: str
    role: str | None
    exact: bool


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # read_text, search_web, view_ticket, back, reload and upload are tools the agent
    # uses to resolve a blocked step by itself before asking the person.
    kind: Literal["click", "fill", "select", "press", "goto", "scroll", "wait", "human", "done",
                  "read_text", "search_web", "view_ticket", "back", "reload", "upload", "ask"]
    target: Target | None
    value: str | None
    reason: str
    risk: Literal["normal", "issue", "captcha", "terms", "login", "unknown"]
    # The agent's running notes: plan, what it tried and what it learned. They are
    # returned on every step so it keeps the whole ticket in mind.
    notes: str | None = None


PLAN_STEPS = 10


class Plan(BaseModel):
    """Everything one consultation decides for the visible screen, in order."""
    model_config = ConfigDict(extra="forbid")
    # From 1 to 10 actions. The bounds are checked here and not declared in the schema
    # sent to the model, like every other answer type of this application.
    steps: list[Action]

    @field_validator('steps')
    @classmethod
    def bounded(cls, value: list[Action]) -> list[Action]:
        if not value:
            raise ValueError('El plan debe traer al menos una acción.')
        return value[:PLAN_STEPS]


# Where the value of a registration field may come from. It is a closed list: the model picks
# a source and the program writes the value, so no secret or personal datum travels to the model.
RegistrationSource = Literal[
    # Tax profile.
    'rfc', 'nombre', 'codigo_postal', 'correo', 'regimen', 'uso_cfdi',
    # «Datos para registros», one per saved key.
    'nombres', 'apellido_paterno', 'apellido_materno', 'telefono', 'fecha_nacimiento', 'calle',
    'numero_exterior', 'numero_interior', 'colonia', 'municipio', 'estado', 'pais',
    # Parts the program derives from them, in the format of the field.
    'nombre_completo', 'apellidos', 'dia_nacimiento', 'mes_nacimiento', 'anio_nacimiento', 'calle_y_numero',
    # Written or decided by the program alone.
    'contrasena_nueva', 'confirmar_contrasena', 'aceptar_terminos', 'opcion', 'omitir', 'falta_dato']


class RegistrationField(BaseModel):
    """The source of one field of a registration form; never its value."""
    model_config = ConfigDict(extra='forbid')
    field: str                      # «c3», as numbered in the description of the form
    source: RegistrationSource
    option: str | None              # with «opcion»: the visible text of one option that was shown
    missing: str | None             # with «falta_dato»: the name of the datum the person did not leave


class RegistrationPlan(BaseModel):
    """What one consultation decides for a registration screen described without values."""
    model_config = ConfigDict(extra='forbid')
    is_registration: bool           # False when the form does not create a user account
    fields: list[RegistrationField]
    submit: str | None              # «b2»: the button that sends this screen, or null when there is none
