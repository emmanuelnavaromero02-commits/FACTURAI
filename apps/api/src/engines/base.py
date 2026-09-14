from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Literal, Optional, Type
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FiscalProfile, Merchant, Ticket, TicketEvent, TipoMotor


class HandoffConcurrenciaExcedidaException(Exception):
    """Excepción lanzada cuando se alcanza HANDOFF_MAX_CONCURRENTES."""
    pass


class HandoffInterface:
    """
    Protocolo de solicitud de intervención humana para resolver captchas o retos en vivo.
    """

    async def request(
        self,
        motivo: str,
        page: Any = None,
        ctx: Any = None,
        submission_attempted: bool = False,
        timeout_seconds: Optional[int] = None,
    ) -> bool:
        """
        Solicita resolución humana. Devuelve True si un humano resolvió a tiempo, False si expiró o se canceló.
        """
        raise NotImplementedError("HandoffInterface base debe implementarse.")


from decimal import Decimal

@dataclass
class EngineResult:
    """Resultado devuelto por cualquier motor de facturación."""

    ok: bool
    cfdi_uuid: Optional[str] = None
    entrega: Literal["emisor", "descarga", "ninguna"] = "ninguna"
    correo_capturado: Optional[str] = None
    pdf: Optional[bytes] = None
    xml: Optional[bytes] = None
    error_code: Optional[str] = None
    mensaje: Optional[str] = None
    reintentable: bool = False

    # Métricas de costo y pasos del agente (Paso B)
    costo_usd: Optional[Decimal] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    pasos: Optional[int] = None
    duracion_segundos: Optional[Decimal] = None


@dataclass
class EngineContext:
    """
    Contexto seguro proporcionado a los motores durante su ejecución.
    Aísla las operaciones y expone utilidades para registrar eventos y solicitar handoff.
    """

    ticket: Ticket
    perfil_fiscal: FiscalProfile
    merchant: Optional[Merchant] = None
    credenciales: Optional[Dict[str, Any]] = None
    handoff: HandoffInterface = field(default_factory=HandoffInterface)
    _session: Optional[AsyncSession] = None
    _tenant_id: Optional[uuid.UUID] = None

    async def log(
        self, tipo: str, mensaje: str, meta: Optional[Dict[str, Any]] = None
    ) -> TicketEvent:
        """Registra un evento seguro en ticket_events en la sesión activa del tenant."""
        if not self._session or not self._tenant_id:
            raise RuntimeError("EngineContext no cuenta con sesión activa para registrar eventos.")

        event = TicketEvent(
            tenant_id=self._tenant_id,
            ticket_id=self.ticket.id,
            tipo=tipo,
            mensaje=mensaje,
            meta=meta or {},
        )
        self._session.add(event)
        await self._session.flush()
        return event


class FacturacionEngine(ABC):
    """Clase base abstracta que debe implementar todo motor de comercio."""

    slug: str
    tipo: TipoMotor

    @abstractmethod
    async def facturar(self, ctx: EngineContext) -> EngineResult:
        """Ejecuta la facturación ante el portal o API correspondiente."""
        raise NotImplementedError


# Registro desacoplado de motores
ENGINE_REGISTRY: Dict[str, Type[FacturacionEngine]] = {}


def register_engine(slug: str) -> Callable[[Type[FacturacionEngine]], Type[FacturacionEngine]]:
    """
    Decorador para registrar motores de facturación por su slug de catálogo.
    Agregar un motor nuevo nunca requiere tocar el worker ni el código central.
    """

    def decorator(cls: Type[FacturacionEngine]) -> Type[FacturacionEngine]:
        cls.slug = slug
        ENGINE_REGISTRY[slug] = cls
        return cls

    return decorator


def get_engine(slug: str) -> Optional[FacturacionEngine]:
    """Obtiene una instancia del motor registrado por su slug."""
    engine_cls = ENGINE_REGISTRY.get(slug)
    if engine_cls:
        return engine_cls()
    return None
