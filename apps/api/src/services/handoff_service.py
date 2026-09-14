import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
from typing import Any, Optional
import uuid
from sqlalchemy import select, text

from ..config import get_settings
from ..db import tenant_session
from ..engines.base import HandoffConcurrenciaExcedidaException, HandoffInterface
from ..models import HandoffEstado, HandoffSession, Ticket, TicketEstado, TicketEvent
from ..state_machine import transition
from .handoff_manager import ActiveHandoffSession, get_handoff_manager

logger = logging.getLogger(__name__)
settings = get_settings()


class RealHandoffInterface(HandoffInterface):
    """Implementación productiva de HandoffInterface usando CDP Screencast y HandoffManager."""

    async def request(
        self,
        motivo: str,
        page: Any = None,
        ctx: Any = None,
        submission_attempted: bool = False,
        timeout_seconds: Optional[int] = None,
    ) -> bool:
        if page is None or ctx is None:
            raise ValueError("page y ctx son requeridos para RealHandoffInterface.")

        handoff_mgr = get_handoff_manager()

        # 1. Comprobar tope de concurrencia (HANDOFF_MAX_CONCURRENTES)
        if handoff_mgr.get_active_count() >= settings.HANDOFF_MAX_CONCURRENTES:
            logger.warning(
                "Tope de handoffs concurrentes alcanzado (%d/%d). Reencolando ticket %s.",
                handoff_mgr.get_active_count(),
                settings.HANDOFF_MAX_CONCURRENTES,
                ctx.ticket.id,
            )
            await ctx.log(
                tipo="handoff_tope_concurrencia",
                mensaje=f"Tope de handoffs concurrentes alcanzado ({settings.HANDOFF_MAX_CONCURRENTES}). Ticket reencolado sin abrir navegador.",
                meta={"motivo": motivo, "max_concurrentes": settings.HANDOFF_MAX_CONCURRENTES},
            )
            raise HandoffConcurrenciaExcedidaException()

        # 2. Generar token efímero de un solo uso
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        handoff_id = uuid.uuid4()
        ttl = timeout_seconds or settings.HANDOFF_TTL_SEGUNDOS
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        # 3. Registrar en BD (tabla handoff_sessions y ticket_events con token)
        async with tenant_session(ctx.ticket.tenant_id) as session:
            handoff_db = HandoffSession(
                id=handoff_id,
                tenant_id=ctx.ticket.tenant_id,
                ticket_id=ctx.ticket.id,
                motivo=motivo,
                estado=HandoffEstado.ESPERANDO,
                token_hash=token_hash,
                expires_at=expires_at,
            )
            session.add(handoff_db)

            event = TicketEvent(
                tenant_id=ctx.ticket.tenant_id,
                ticket_id=ctx.ticket.id,
                tipo="handoff",
                mensaje=f"Intervención humana solicitada: {motivo}.",
                meta={
                    "handoff_id": str(handoff_id),
                    "token": raw_token,
                    "motivo": motivo,
                    "expires_at": expires_at.isoformat(),
                    "ttl_segundos": ttl,
                },
            )
            session.add(event)
            await session.commit()

        # 5. Transición del ticket a ESPERA_HUMANO
        async with tenant_session(ctx.ticket.tenant_id) as session:
            res = await session.execute(select(Ticket).where(Ticket.id == ctx.ticket.id))
            t_obj = res.scalar_one()
            await transition(
                session,
                t_obj,
                TicketEstado.ESPERA_HUMANO,
                f"En espera de intervención humana para resolver {motivo}.",
                meta={"handoff_id": str(handoff_id), "motivo": motivo},
            )
            await session.commit()

        # 6. Registrar en HandoffManager e iniciar screencast CDP y listener de control remoto
        active_session = ActiveHandoffSession(
            handoff_id=handoff_id,
            tenant_id=ctx.ticket.tenant_id,
            ticket_id=ctx.ticket.id,
            page=page,
            motivo=motivo,
            submission_attempted=submission_attempted,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        handoff_mgr.register_session(active_session)
        await handoff_mgr.start_cdp_screencast(active_session)
        control_task = asyncio.create_task(handoff_mgr.listen_remote_control(active_session))

        # 7. Esperar resolución, cancelación o vencimiento de TTL
        try:
            resolved_task = asyncio.create_task(active_session.event_resolved.wait())
            cancelled_task = asyncio.create_task(active_session.event_cancelled.wait())

            done, pending = await asyncio.wait(
                [resolved_task, cancelled_task],
                timeout=float(ttl),
                return_when=asyncio.FIRST_COMPLETED,
            )

            for p in pending:
                p.cancel()

            if active_session.event_resolved.is_set():
                async with tenant_session(ctx.ticket.tenant_id) as session:
                    res = await session.execute(select(HandoffSession).where(HandoffSession.id == handoff_id))
                    h_record = res.scalar_one_or_none()
                    if h_record:
                        h_record.estado = HandoffEstado.RESUELTO
                    t_res = await session.execute(select(Ticket).where(Ticket.id == ctx.ticket.id))
                    t_obj = t_res.scalar_one()
                    await transition(
                        session,
                        t_obj,
                        TicketEstado.FACTURANDO,
                        f"Intervención humana completada para {motivo}. El agente continúa.",
                        meta={"handoff_id": str(handoff_id), "motivo": motivo},
                    )
                    await session.commit()
                ctx.ticket.estado = TicketEstado.FACTURANDO
                return True

            elif active_session.event_cancelled.is_set():
                async with tenant_session(ctx.ticket.tenant_id) as session:
                    res = await session.execute(select(HandoffSession).where(HandoffSession.id == handoff_id))
                    h_record = res.scalar_one_or_none()
                    if h_record:
                        h_record.estado = HandoffEstado.CANCELADO
                        await session.commit()
                return False

            else:
                active_session.is_expired = True
                async with tenant_session(ctx.ticket.tenant_id) as session:
                    res = await session.execute(select(HandoffSession).where(HandoffSession.id == handoff_id))
                    h_record = res.scalar_one_or_none()
                    if h_record:
                        h_record.estado = HandoffEstado.EXPIRADO
                        await session.commit()
                return False

        finally:
            control_task.cancel()
            await handoff_mgr.stop_cdp_screencast(active_session)
            handoff_mgr.remove_session(handoff_id)
