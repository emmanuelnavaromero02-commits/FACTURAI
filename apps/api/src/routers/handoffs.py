import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Dict, Optional
import uuid
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select, text

from ..auth import verify_session_token
from ..config import get_settings
from ..db import admin_session
from ..models import HandoffEstado, HandoffSession, Membership, MembershipRole, Ticket
from ..redis_client import get_redis_client
from ..services.handoff_manager import get_handoff_manager

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["Handoffs"])

# Registro de WebSockets remotos activos para garantizar que 'una segunda conexión cierra la primera'
_remote_active_connections: Dict[uuid.UUID, WebSocket] = {}


@router.websocket("/v1/handoffs/{handoff_id}/live")
async def handoff_live_websocket(
    websocket: WebSocket,
    handoff_id: uuid.UUID,
    token: str = Query(..., description="Token efímero de un solo uso entregado en el evento de handoff"),
    session_token: Optional[str] = Query(None, description="Token de sesión opcional vía query param"),
):
    """
    WebSocket interactivo bidireccional para intervención humana en vivo.
    Valida: token vigente, sesión en estado esperando, y que el usuario sea dueño del ticket o admin del tenant.
    Una segunda conexión cierra la primera automáticamente.
    """
    # Aceptar la conexión para emitir códigos y razones de cierre detallados
    await websocket.accept()

    # 1. Determinar cookie o token de sesión del usuario
    raw_session = websocket.cookies.get(settings.SESSION_COOKIE_NAME) or session_token
    if not raw_session:
        auth_header = websocket.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw_session = auth_header.split(" ", 1)[1]

    if not raw_session:
        # No autenticado
        await websocket.close(code=4001, reason="No autenticado")
        return

    try:
        payload, _ = verify_session_token(raw_session)
        user_id = uuid.UUID(payload.get("sub"))
    except Exception:
        await websocket.close(code=4001, reason="Sesión inválida o expirada")
        return

    # 2. Validar token de handoff y sesión en base de datos
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)

    async with admin_session() as session_db:
        res = await session_db.execute(
            select(HandoffSession).where(HandoffSession.id == handoff_id)
        )
        handoff_record = res.scalar_one_or_none()

        if not handoff_record or handoff_record.token_hash != token_hash:
            await websocket.close(code=4001, reason="Token de handoff inválido")
            return

        if handoff_record.expires_at <= now:
            await websocket.close(code=4001, reason="Sesión de handoff expirada")
            return

        if handoff_record.estado not in (HandoffEstado.ESPERANDO,):
            await websocket.close(code=4001, reason=f"Sesión no disponible (estado: {handoff_record.estado.value})")
            return

        # 3. Validar permisos del usuario sobre el tenant y ticket
        await session_db.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(handoff_record.tenant_id)},
        )

        # Comprobar pertenencia al tenant
        mem_res = await session_db.execute(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.tenant_id == handoff_record.tenant_id,
            )
        )
        membership = mem_res.scalar_one_or_none()
        if not membership:
            await websocket.close(code=4003, reason="Usuario no pertenece a este tenant")
            return

        # Comprobar si es admin/owner o creador del ticket
        ticket_res = await session_db.execute(
            select(Ticket).where(Ticket.id == handoff_record.ticket_id)
        )
        ticket_obj = ticket_res.scalar_one_or_none()
        if not ticket_obj:
            await websocket.close(code=4003, reason="Ticket no encontrado")
            return

        is_admin_or_owner = membership.rol in (MembershipRole.OWNER, MembershipRole.ADMIN)
        is_creator = ticket_obj.created_by == user_id
        if not (is_admin_or_owner or is_creator):
            await websocket.close(code=4003, reason="Permisos insuficientes para resolver este ticket")
            return

    # 4. Conectar y asociar al HandoffManager (Local o Remoto vía Redis)
    handoff_mgr = get_handoff_manager()
    active_session = handoff_mgr.get_session(handoff_id)

    if active_session:
        # Modo en el mismo proceso (Local / Test unitario)
        await handoff_mgr.attach_websocket(handoff_id, websocket)

        await websocket.send_json({
            "t": "state",
            "v": "esperando",
            "submission_attempted": active_session.submission_attempted,
        })

        if active_session.last_frame_b64:
            try:
                await websocket.send_json({
                    "t": "frame",
                    "seq": active_session.seq,
                    "jpeg": active_session.last_frame_b64,
                    "w": 1280,
                    "h": 800,
                })
            except Exception:
                pass

        try:
            while True:
                msg = await websocket.receive_json()
                await handoff_mgr.handle_client_message(handoff_id, msg)
                if active_session.event_resolved.is_set():
                    try:
                        await websocket.send_json({"t": "done", "ok": True})
                    except Exception:
                        pass
                    break
                if active_session.event_cancelled.is_set():
                    break
        except WebSocketDisconnect:
            logger.info("WebSocket local de handoff %s desconectado por el cliente.", handoff_id)
            await handoff_mgr.detach_websocket(handoff_id, websocket)
        except Exception as exc:
            logger.warning("Error en WebSocket local de handoff %s: %s", handoff_id, exc)
            await handoff_mgr.detach_websocket(handoff_id, websocket)
        return

    # Modo multi-proceso: la sesión está en el Worker y FastAPI se comunica vía Redis
    redis = get_redis_client()
    meta_str = None
    # Esperar hasta 3s en caso de pequeña carrera mientras el worker inicia el screencast
    for _ in range(10):
        meta_str = await redis.get(f"handoff:{handoff_id}:meta")
        if meta_str:
            break
        await asyncio.sleep(0.3)

    if not meta_str:
        await websocket.close(code=1011, reason="Sesión no activa en el worker")
        return

    meta = json.loads(meta_str)
    submission_attempted = meta.get("submission_attempted", False)

    # Regla: 'Una segunda conexión cierra la primera'
    prev_ws = _remote_active_connections.get(handoff_id)
    if prev_ws and prev_ws != websocket:
        logger.info("Cerrando conexión remota previa para handoff %s por nueva conexión.", handoff_id)
        try:
            await prev_ws.close(code=1000, reason="superseded")
        except Exception:
            pass
    _remote_active_connections[handoff_id] = websocket

    pubsub = redis.pubsub()
    stream_channel = f"handoff:{handoff_id}:stream"
    await pubsub.subscribe(stream_channel)

    # Notificar al worker que el WebSocket está conectado
    await redis.publish(f"handoff:{handoff_id}:control", json.dumps({"t": "ws_connected"}))

    # Enviar estado inicial y frame en caché si existe
    await websocket.send_json({
        "t": "state",
        "v": "esperando",
        "submission_attempted": submission_attempted,
    })

    last_frame = None
    for _ in range(10):
        last_frame = await redis.get(f"handoff:{handoff_id}:last_frame")
        if last_frame:
            break
        await asyncio.sleep(0.15)

    if last_frame:
        try:
            await websocket.send_json({
                "t": "frame",
                "seq": 1,
                "jpeg": last_frame,
                "w": 1280,
                "h": 800,
            })
        except Exception:
            pass

    async def _forward_stream():
        try:
            async for s_msg in pubsub.listen():
                if s_msg.get("type") == "message":
                    data = s_msg.get("data")
                    if data:
                        await websocket.send_text(data)
                        try:
                            parsed = json.loads(data)
                            if parsed.get("t") in ("done", "cancel"):
                                break
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Error forward stream a WS: %s", exc)

    async def _forward_client_control():
        try:
            while True:
                c_msg = await websocket.receive_json()
                await redis.publish(f"handoff:{handoff_id}:control", json.dumps(c_msg))
                tipo = c_msg.get("t")
                if tipo == "cancel":
                    break
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.debug("Error forward WS a control: %s", exc)

    stream_task = asyncio.create_task(_forward_stream())
    control_task = asyncio.create_task(_forward_client_control())

    try:
        done, pending = await asyncio.wait(
            [stream_task, control_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
    finally:
        stream_task.cancel()
        control_task.cancel()
        try:
            await pubsub.unsubscribe(stream_channel)
            await pubsub.aclose()
        except Exception:
            pass
        if _remote_active_connections.get(handoff_id) == websocket:
            _remote_active_connections.pop(handoff_id, None)
        try:
            await redis.publish(f"handoff:{handoff_id}:control", json.dumps({"t": "ws_disconnected"}))
        except Exception:
            pass
