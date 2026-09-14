import uuid
from typing import ClassVar

from ..models import TipoMotor
from .base import EngineContext, EngineResult, FacturacionEngine, register_engine


@register_engine("mock")
class MockFacturacionEngine(FacturacionEngine):
    """
    Motor simulador para pruebas de flujo completo sin tocar portales reales.
    Permite ejercitar éxito, captcha, rechazos, portal caído y fallas críticas
    según el folio del ticket.
    """

    slug = "mock"
    tipo = TipoMotor.API

    # Contador para verificar que carreras de workers no ejecutan el motor dos veces
    invocations_count: ClassVar[int] = 0

    @classmethod
    def reset_invocations_count(cls) -> None:
        cls.invocations_count = 0

    async def facturar(self, ctx: EngineContext) -> EngineResult:
        MockFacturacionEngine.invocations_count += 1
        folio = (ctx.ticket.folio or "").strip().upper()

        await ctx.log("motor_iniciado", f"Iniciando procesamiento con motor simulado para folio {folio}.")

        # 1. Simulación de portal que pide colonia para Regla 10 (acotado a PERFIL-INCOMPLETO)
        if "PERFIL-INCOMPLETO" in folio:
            if not ctx.perfil_fiscal or not ctx.perfil_fiscal.colonia:
                await ctx.log("perfil_incompleto", "El portal solicita la colonia y no se encuentra en el perfil fiscal.")
                return EngineResult(
                    ok=False,
                    error_code="perfil_incompleto",
                    mensaje=f"El portal de {ctx.merchant.nombre} pide tu colonia y no la tienes en tu perfil. Agrégala y reintenta.",
                    reintentable=True,
                )

        # 2. Simulación de captcha con solicitud de handoff
        if "CAPTCHA" in folio:
            await ctx.log("captcha_detectado", "El portal solicitó resolver un captcha interactivo.")
            resuelto = await ctx.handoff.request("Resolver captcha del portal", timeout=120)
            if not resuelto:
                return EngineResult(
                    ok=False,
                    error_code="captcha_no_resuelto",
                    mensaje="El reto humano expiró o no pudo ser completado a tiempo.",
                    reintentable=True,
                )

        # 3. Simulación de folio inexistente o rechazado (error definitivo)
        if "RECHAZADO" in folio:
            await ctx.log("folio_rechazado", "El comercio rechazó el ticket: folio no localizado.")
            return EngineResult(
                ok=False,
                error_code="folio_rechazado",
                mensaje="El folio proporcionado no fue encontrado en los registros del comercio.",
                reintentable=False,
            )

        # 4. Simulación de portal caído o 503 (error reintentable con backoff)
        if "CAIDO" in folio:
            await ctx.log("portal_caido", "Fallo de conexión: el servidor del comercio arrojó 503.")
            return EngineResult(
                ok=False,
                error_code="portal_caido",
                mensaje="El portal de facturación del comercio no se encuentra disponible temporalmente.",
                reintentable=True,
            )

        # 5. Simulación de credenciales inválidas (error definitivo)
        if "CREDENCIALES" in folio:
            await ctx.log("credenciales_invalidas", "Fallo de autenticación con el portal.")
            return EngineResult(
                ok=False,
                error_code="credenciales_invalidas",
                mensaje="Las credenciales registradas para este portal son incorrectas o fueron revocadas.",
                reintentable=False,
            )

        # 6. Simulación de error de excepción inesperada en el motor (para probar finally)
        if "ERROR-MOTOR" in folio:
            await ctx.log("error_critico_motor", "Ocurrió una excepción fatal dentro del motor.")
            raise RuntimeError("Excepción catastrófica e inesperada durante la ejecución del motor")

        # 7. Caso de éxito: determinar tipo de entrega
        cfdi_uuid = str(uuid.uuid4())
        await ctx.log(
            "cfdi_generado",
            f"CFDI emitido exitosamente con folio fiscal {cfdi_uuid}.",
            meta={"cfdi_uuid": cfdi_uuid},
        )

        email_receptor = ctx.perfil_fiscal.email_receptor if ctx.perfil_fiscal else None

        if "DESCARGA" in folio:
            pdf_bytes = b"%PDF-1.4 \nMOCK CFDI PDF CONTENT " + cfdi_uuid.encode()
            xml_bytes = (
                b'<?xml version="1.0" encoding="UTF-8"?>\n<cfdi:Comprobante UUID="'
                + cfdi_uuid.encode()
                + b'" Total="'
                + str(ctx.ticket.total or "0.00").encode()
                + b'"/>'
            )
            return EngineResult(
                ok=True,
                cfdi_uuid=cfdi_uuid,
                entrega="descarga",
                correo_capturado=email_receptor,
                pdf=pdf_bytes,
                xml=xml_bytes,
            )

        if "NINGUNA" in folio:
            return EngineResult(
                ok=True,
                cfdi_uuid=cfdi_uuid,
                entrega="ninguna",
            )

        # Por defecto: "emisor" (el comercio envía la factura al correo capturado)
        return EngineResult(
            ok=True,
            cfdi_uuid=cfdi_uuid,
            entrega="emisor",
            correo_capturado=email_receptor,
        )
