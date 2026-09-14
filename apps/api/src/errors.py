from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppException(Exception):
    """Excepción base del sistema Facturia con formato de error estandarizado."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: Optional[Dict[str, Any]] = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(self.message)


class NoAutenticadoException(AppException):
    def __init__(
        self,
        message: str = "Inicia sesión con tu cuenta de Google para poder continuar.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="no_autenticado",
            message=message,
            detail=detail,
        )


class SinPermisoException(AppException):
    def __init__(
        self,
        message: str = "No tienes acceso a esta empresa o acción. Solicita permiso a un administrador.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            code="sin_permiso",
            message=message,
            detail=detail,
        )


class TenantRequeridoException(AppException):
    def __init__(
        self,
        message: str = "Perteneces a varias empresas. Especifica la cabecera 'X-Tenant-Id' con el identificador de la empresa a consultar.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="tenant_requerido",
            message=message,
            detail=detail,
        )


class RolInsuficienteException(AppException):
    def __init__(
        self,
        message: str = "Tu rol actual no te permite realizar esta operación en el equipo.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            code="rol_insuficiente",
            message=message,
            detail=detail,
        )


class TokenInvalidoException(AppException):
    def __init__(
        self,
        message: str = "El token de autenticación no es válido o ha expirado. Vuelve a iniciar sesión.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="token_invalido",
            message=message,
            detail=detail,
        )


class EmailNoVerificadoException(AppException):
    def __init__(
        self,
        message: str = "Tu cuenta de Google no tiene el correo electrónico verificado. Verifica tu correo en tu cuenta de Google e inténtalo de nuevo.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="email_no_verificado",
            message=message,
            detail=detail,
        )


class UltimoOwnerException(AppException):
    def __init__(
        self,
        message: str = "No puedes cambiar tu rol ni salir de la empresa porque eres el único dueño. Asigna a otro dueño antes de cambiarte.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="ultimo_owner",
            message=message,
            detail=detail,
        )


class RolSuperiorNoPermitidoException(AppException):
    def __init__(
        self,
        message: str = "No puedes asignar un rol igual o superior al que tú tienes en esta empresa.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            code="rol_superior_no_permitido",
            message=message,
            detail=detail,
        )


class TipoArchivoInvalidoException(AppException):
    def __init__(
        self,
        message: str = "El archivo enviado no es un formato válido. Aceptamos imágenes JPEG, PNG, HEIC, WebP y documentos PDF.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="tipo_archivo_no_soportado",
            message=message,
            detail=detail,
        )


class ArchivoDemasiadoGrandeException(AppException):
    def __init__(
        self,
        message: str = "El archivo supera el tamaño máximo permitido de 10 MB. Toma una foto más ligera o comprime el documento.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="archivo_demasiado_grande",
            message=message,
            detail=detail,
        )


class RecursoNoEncontradoException(AppException):
    def __init__(
        self,
        message: str = "El registro o recurso solicitado no fue encontrado. Verifica los identificadores.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            code="no_encontrado",
            message=message,
            detail=detail,
        )


class ReintentoInvalidoException(AppException):
    def __init__(
        self,
        message: str = "Solo se pueden reintentar tickets en estado 'rechazado'.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="reintento_invalido",
            message=message,
            detail=detail,
        )


class MaximoIntentosExcedidoException(AppException):
    def __init__(
        self,
        message: str = "El ticket ha alcanzado el límite máximo de 3 intentos permitidos.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="maximo_intentos_excedido",
            message=message,
            detail=detail,
        )


class CfdiExpiradoException(AppException):
    def __init__(
        self,
        message: str = (
            "El archivo de este CFDI ya expiró o superó el límite de descargas. "
            "Por política de privacidad y seguridad no conservamos copias permanentes de tus comprobantes. "
            "Puedes consultar o descargar tu factura directamente en el portal del SAT o del comercio emisor."
        ),
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            code="cfdi_expirado",
            message=message,
            detail=detail,
        )


class PerfilIncompletoException(AppException):
    def __init__(
        self,
        message: str = "Faltan datos fiscales requeridos por el comercio en el perfil fiscal del tenant.",
        detail: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="perfil_incompleto",
            message=message,
            detail=detail,
        )


def register_error_handlers(app: FastAPI) -> None:
    """Registra los manejadores de error para que toda la API responda en formato uniforme."""

    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validacion_fallida",
                    "message": "Los datos enviados no son válidos. Revisa los campos señalados e intenta de nuevo.",
                    "detail": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        code_map = {
            401: "no_autenticado",
            403: "sin_permiso",
            404: "no_encontrado",
            405: "metodo_no_permitido",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code_map.get(exc.status_code, "error_http"),
                    "message": str(exc.detail),
                    "detail": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # En producción no revelamos detalles internos
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "error_servidor",
                    "message": "Ocurrió un problema temporal al procesar tu solicitud. Por favor intenta de nuevo en unos momentos.",
                    "detail": {},
                }
            },
        )
