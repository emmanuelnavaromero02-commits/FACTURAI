import logging
from email.message import EmailMessage
from typing import Callable, Optional
import aiosmtplib

from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

EmailSenderType = Callable[[str, str, bytes, bytes, str, str], None]
_email_sender_override: Optional[EmailSenderType] = None


def set_email_sender(sender: Optional[EmailSenderType]) -> None:
    """Permite sobrescribir el despachador de correo en tests."""
    global _email_sender_override
    _email_sender_override = sender


async def send_cfdi_email(
    to_email: str,
    cfdi_uuid: str,
    pdf_bytes: bytes,
    xml_bytes: bytes,
    rfc_receptor: str = "",
    razon_social: str = "",
) -> None:
    """
    Envía los comprobantes CFDI 4.0 (PDF y XML) al correo del receptor vía SMTP.
    Regla 4: Ni el PDF ni el XML se persisten en base de datos, disco ni bucket.
    Los búferes se entregan al protocolo SMTP y se descartan en este mismo ámbito.
    """
    if _email_sender_override is not None:
        await _email_sender_override(to_email, cfdi_uuid, pdf_bytes, xml_bytes, rfc_receptor, razon_social)
        return

    msg = EmailMessage()
    msg["Subject"] = f"Tu factura electrónica CFDI - {cfdi_uuid[:8]}"
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to_email

    body_text = (
        f"Hola,\n\n"
        f"Adjunto a este correo encontrarás tu factura electrónica (CFDI 4.0) correspondiente a tu compra.\n\n"
        f"Folio Fiscal (UUID): {cfdi_uuid}\n"
        f"Receptor: {razon_social} ({rfc_receptor})\n\n"
        f"Generado automáticamente por Facturia."
    )
    msg.set_content(body_text)

    # Adjuntar PDF
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=f"{cfdi_uuid}.pdf",
    )

    # Adjuntar XML
    msg.add_attachment(
        xml_bytes,
        maintype="application",
        subtype="xml",
        filename=f"{cfdi_uuid}.xml",
    )

    await aiosmtplib.send(
        msg,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD,
        use_tls=False,
    )

    logger.info("Correo con CFDI %s enviado exitosamente a %s", cfdi_uuid, to_email)
