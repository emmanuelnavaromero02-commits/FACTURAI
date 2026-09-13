from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import AppException
from .models import Ticket, TicketEstado, TicketEvent

FINAL_STATES: Set[TicketEstado] = {
    TicketEstado.FACTURADO,
    TicketEstado.RECHAZADO,
    TicketEstado.CANCELADO,
}

VALID_TRANSITIONS: Dict[TicketEstado, Set[TicketEstado]] = {
    TicketEstado.RECIBIDO: {
        TicketEstado.EXTRAYENDO,
        TicketEstado.CANCELADO,
    },
    TicketEstado.EXTRAYENDO: {
        TicketEstado.EXTRAIDO,
        TicketEstado.RECHAZADO,
        TicketEstado.CANCELADO,
    },
    TicketEstado.EXTRAIDO: {
        TicketEstado.ENCOLADO,
        TicketEstado.RECHAZADO,
        TicketEstado.CANCELADO,
    },
    TicketEstado.ENCOLADO: {
        TicketEstado.FACTURANDO,
        TicketEstado.CANCELADO,
    },
    TicketEstado.FACTURANDO: {
        TicketEstado.FACTURADO,
        TicketEstado.ENCOLADO,
        TicketEstado.ESPERA_HUMANO,
        TicketEstado.RECHAZADO,
        TicketEstado.CANCELADO,
    },
    TicketEstado.ESPERA_HUMANO: {
        TicketEstado.FACTURANDO,
        TicketEstado.ENCOLADO,
        TicketEstado.CANCELADO,
    },
    TicketEstado.FACTURADO: set(),
    # Rechazado permite reintento explícito a encolado respetando límite de intentos
    TicketEstado.RECHAZADO: {TicketEstado.ENCOLADO},
    TicketEstado.CANCELADO: set(),
}


class TransicionInvalida(AppException):
    def __init__(self, estado_actual: TicketEstado, nuevo_estado: TicketEstado):
        super().__init__(
            status_code=400,
            code="transicion_invalida",
            message=f"No es posible cambiar el ticket de '{estado_actual.value}' a '{nuevo_estado.value}'.",
            detail={
                "estado_actual": estado_actual.value,
                "nuevo_estado": nuevo_estado.value,
            },
        )


async def transition(
    session: AsyncSession,
    ticket: Ticket,
    nuevo_estado: TicketEstado,
    mensaje: str,
    meta: Optional[Dict[str, Any]] = None,
    tipo: str = "cambio_estado",
) -> TicketEvent:
    """
    Función obligatoria para transiciones de estado de un ticket.
    Valida que la transición sea permitida según el grafo de estados,
    actualiza el estado del ticket y genera el registro en ticket_events.
    NADIE asigna ticket.estado manualmente en el código.
    """
    allowed_next = VALID_TRANSITIONS.get(ticket.estado, set())
    if nuevo_estado not in allowed_next:
        raise TransicionInvalida(ticket.estado, nuevo_estado)

    ticket.estado = nuevo_estado

    if nuevo_estado == TicketEstado.FACTURADO and not ticket.facturado_at:
        ticket.facturado_at = datetime.now(timezone.utc)

    event = TicketEvent(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        tipo=tipo,
        mensaje=mensaje,
        meta=meta or {},
    )
    session.add(event)
    await session.flush()
    return event
