import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from typing import Dict, Optional
import uuid
from fastapi import WebSocket
from playwright.async_api import CDPSession, Page

from ..config import get_settings
from ..redis_client import get_redis_client

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class ActiveHandoffSession:
    """Sesión de handoff humano activa en memoria en el worker."""

    handoff_id: uuid.UUID
    tenant_id: uuid.UUID
    ticket_id: uuid.UUID
    page: Page
    motivo: str
    submission_attempted: bool
    created_at: datetime
    expires_at: datetime

    cdp: Optional[CDPSession] = None
    active_ws: Optional[WebSocket] = None
    event_resolved: asyncio.Event = field(default_factory=asyncio.Event)
    event_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    seq: int = 0
    last_frame_b64: Optional[str] = None
    disconnect_timer_task: Optional[asyncio.Task] = None
    periodic_task: Optional[asyncio.Task] = None
    is_abandoned: bool = False
    is_expired: bool = False


class HandoffManager:
    """Gestor singleton en memoria para puentear Playwright en el worker con WebSockets en FastAPI."""

    def __init__(self):
        self._sessions: Dict[uuid.UUID, ActiveHandoffSession] = {}
        self._lock = asyncio.Lock()

    def get_active_count(self) -> int:
        """Devuelve el número de sesiones de handoff actualmente vivas en Chromium."""
        now = datetime.now(timezone.utc)
        count = 0
        for s in self._sessions.values():
            if not s.is_expired and not s.event_resolved.is_set() and not s.event_cancelled.is_set():
                if s.expires_at > now:
                    count += 1
        return count

    def register_session(self, session: ActiveHandoffSession) -> None:
        self._sessions[session.handoff_id] = session
        logger.info(
            "Handoff registrado %s (ticket=%s, motivo=%s). Activos=%d",
            session.handoff_id,
            session.ticket_id,
            session.motivo,
            self.get_active_count(),
        )

    def get_session(self, handoff_id: uuid.UUID) -> Optional[ActiveHandoffSession]:
        return self._sessions.get(handoff_id)

    def remove_session(self, handoff_id: uuid.UUID) -> None:
        session = self._sessions.pop(handoff_id, None)
        if session:
            if session.disconnect_timer_task and not session.disconnect_timer_task.done():
                session.disconnect_timer_task.cancel()

    async def attach_websocket(self, handoff_id: uuid.UUID, ws: WebSocket) -> ActiveHandoffSession:
        """
        Asocia un WebSocket a la sesión de handoff.
        REGLA: 'Una segunda conexión cierra la primera'.
        Cancela el contador de abandono si estaba corriendo.
        """
        async with self._lock:
            session = self.get_session(handoff_id)
            if not session:
                raise ValueError("Sesión de handoff no encontrada en el worker.")

            # Si ya hay una conexión activa previa, cerrarla limpiamente
            if session.active_ws and session.active_ws != ws:
                logger.info("Cerrando conexión WebSocket previa de handoff %s por nueva conexión.", handoff_id)
                try:
                    await session.active_ws.close(code=1000, reason="superseded")
                except Exception:
                    pass

            # Cancelar temporizador de desconexión / abandono si existía
            if session.disconnect_timer_task and not session.disconnect_timer_task.done():
                session.disconnect_timer_task.cancel()
                session.disconnect_timer_task = None

            session.active_ws = ws
            return session

    async def detach_websocket(self, handoff_id: uuid.UUID, ws: WebSocket) -> None:
        """
        Maneja la desconexión del WebSocket.
        REGLA DE ABANDONO: Si no se reconecta en 20 segundos, trata la sesión como abandonada,
        cierra el navegador y reencola para no retener la memoria de Chromium viva.
        """
        async with self._lock:
            session = self.get_session(handoff_id)
            if not session:
                return

            if session.active_ws == ws:
                session.active_ws = None

                # Si no está resuelta ni cancelada previamente, arrancar temporizador de 90s
                if not session.event_resolved.is_set() and not session.event_cancelled.is_set():
                    async def _abandon_timer():
                        try:
                            await asyncio.sleep(90)
                            async with self._lock:
                                if session.active_ws is None and not session.event_resolved.is_set():
                                    logger.warning(
                                        "Handoff %s abandonado: 90s sin reconexión de WebSocket. Liberando Chromium.",
                                        handoff_id,
                                    )
                                    session.is_abandoned = True
                                    session.event_cancelled.set()
                        except asyncio.CancelledError:
                            pass

                    session.disconnect_timer_task = asyncio.create_task(_abandon_timer())

    async def capture_and_publish_frame(self, session: ActiveHandoffSession) -> Optional[str]:
        """Captura un screenshot directo de Chromium y lo publica a Redis y WebSocket."""
        try:
            if session.page and not session.page.is_closed():
                shot_bytes = await session.page.screenshot(type="jpeg", quality=75)
                shot_b64 = base64.b64encode(shot_bytes).decode("utf-8")
                session.last_frame_b64 = shot_b64
                session.seq += 1
                payload = json.dumps({
                    "t": "frame",
                    "seq": session.seq,
                    "jpeg": shot_b64,
                    "w": 1280,
                    "h": 800,
                })
                r = get_redis_client()
                await r.set(
                    f"handoff:{session.handoff_id}:last_frame",
                    shot_b64,
                    ex=settings.HANDOFF_TTL_SEGUNDOS,
                )
                await r.publish(f"handoff:{session.handoff_id}:stream", payload)
                if session.active_ws:
                    try:
                        await session.active_ws.send_text(payload)
                    except Exception:
                        pass
                return shot_b64
        except Exception as exc:
            logger.debug("Error capturando frame directo para handoff %s: %s", session.handoff_id, exc)
        return None

    async def handle_client_message(self, handoff_id: uuid.UUID, msg: dict) -> None:
        """
        Procesa mensajes enviados por el cliente:
        - click: {t: 'click', x: float, y: float} (0..1)
        - key: {t: 'key', k: str}
        - text: {t: 'text', v: str}
        - done: {t: 'done'}
        - cancel: {t: 'cancel'}
        """
        session = self.get_session(handoff_id)
        if not session:
            return

        tipo = msg.get("t")
        if tipo == "click":
            x = float(msg.get("x", 0.0))
            y = float(msg.get("y", 0.0))
            # Normalizado a viewport 1280x800
            actual_x = max(0.0, min(1280.0, x * 1280.0))
            actual_y = max(0.0, min(800.0, y * 800.0))
            await session.page.mouse.click(actual_x, actual_y)
            await asyncio.sleep(0.25)
            await self.capture_and_publish_frame(session)

        elif tipo == "key":
            key_name = str(msg.get("k", ""))
            if key_name:
                await session.page.keyboard.press(key_name)
                await asyncio.sleep(0.2)
                await self.capture_and_publish_frame(session)

        elif tipo == "text":
            texto = str(msg.get("v", ""))
            if texto:
                await session.page.keyboard.type(texto)
                await asyncio.sleep(0.2)
                await self.capture_and_publish_frame(session)

        elif tipo == "done":
            logger.info("Cliente indicó reto resuelto para handoff %s", handoff_id)
            session.event_resolved.set()
            if session.active_ws:
                try:
                    await session.active_ws.send_json({"t": "done", "ok": True})
                except Exception:
                    pass

        elif tipo == "cancel":
            logger.info("Cliente canceló la sesión de handoff %s", handoff_id)
            session.event_cancelled.set()

    async def start_cdp_screencast(self, session: ActiveHandoffSession) -> None:
        """Inicia el screencast CDP en Chromium y publica frames en Redis Pub/Sub."""
        # Registrar metadatos en Redis para que el proceso de FastAPI/Uvicorn sepa que la sesión está activa
        try:
            redis = get_redis_client()
            meta_json = json.dumps({
                "handoff_id": str(session.handoff_id),
                "tenant_id": str(session.tenant_id),
                "ticket_id": str(session.ticket_id),
                "motivo": session.motivo,
                "submission_attempted": session.submission_attempted,
                "active": True,
            })
            await redis.set(
                f"handoff:{session.handoff_id}:meta",
                meta_json,
                ex=settings.HANDOFF_TTL_SEGUNDOS + 60,
            )
        except Exception as exc:
            logger.warning("No se pudo registrar metadatos en Redis para handoff %s: %s", session.handoff_id, exc)

        # Captura inmediata garantizada para que last_frame exista desde el primer milisegundo
        await self.capture_and_publish_frame(session)

        try:
            cdp = await session.page.context.new_cdp_session(session.page)
            session.cdp = cdp

            async def _on_screencast_frame(event: dict):
                session_id = event.get("sessionId")
                data_b64 = event.get("data")
                session.last_frame_b64 = data_b64
                session.seq += 1

                # 1. Envío local si hay WebSocket directo en este proceso
                if session.active_ws:
                    try:
                        await session.active_ws.send_json({
                            "t": "frame",
                            "seq": session.seq,
                            "jpeg": data_b64,
                            "w": 1280,
                            "h": 800,
                        })
                    except Exception:
                        pass

                # 2. Publicación a Redis para procesos remotos (FastAPI/Uvicorn)
                try:
                    r = get_redis_client()
                    frame_payload = json.dumps({
                        "t": "frame",
                        "seq": session.seq,
                        "jpeg": data_b64,
                        "w": 1280,
                        "h": 800,
                    })
                    await r.set(f"handoff:{session.handoff_id}:last_frame", data_b64, ex=settings.HANDOFF_TTL_SEGUNDOS)
                    await r.publish(f"handoff:{session.handoff_id}:stream", frame_payload)
                except Exception as exc:
                    logger.debug("Error publicando frame a Redis para handoff %s: %s", session.handoff_id, exc)

                try:
                    await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                except Exception:
                    pass

            cdp.on("Page.screencastFrame", lambda ev: asyncio.create_task(_on_screencast_frame(ev)))

            await cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 60,
                    "maxWidth": 1280,
                    "maxHeight": 800,
                    "everyNthFrame": 2,
                },
            )
        except Exception as exc:
            logger.warning("No se pudo iniciar CDP screencast para handoff %s: %s", session.handoff_id, exc)

        # Bucle periódico de refresco cada 1.0s para páginas estáticas o antibots
        async def _periodic_refresh():
            while not session.event_resolved.is_set() and not session.event_cancelled.is_set() and not session.is_expired:
                try:
                    await asyncio.sleep(1.0)
                    await self.capture_and_publish_frame(session)
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass

        session.periodic_task = asyncio.create_task(_periodic_refresh())

    async def listen_remote_control(self, session: ActiveHandoffSession) -> None:
        """
        Escucha eventos de control (clicks, typing, done, cancel, ws_connected, ws_disconnected)
        desde Redis Pub/Sub publicados por Uvicorn u otros procesos.
        """
        channel = f"handoff:{session.handoff_id}:control"
        pubsub = None
        try:
            r = get_redis_client()
            pubsub = r.pubsub()
            await pubsub.subscribe(channel)
            logger.info("Worker suscrito al canal de control %s para handoff %s", channel, session.handoff_id)
            while not session.event_resolved.is_set() and not session.event_cancelled.is_set() and not session.is_expired:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                if msg and msg.get("type") == "message":
                    raw_data = msg.get("data")
                    if raw_data:
                        try:
                            payload = json.loads(raw_data)
                            tipo = payload.get("t")
                            if tipo == "ws_connected":
                                logger.info("WebSocket remoto conectado para handoff %s. Cancelando temporizador de abandono.", session.handoff_id)
                                if session.disconnect_timer_task and not session.disconnect_timer_task.done():
                                    session.disconnect_timer_task.cancel()
                                    session.disconnect_timer_task = None
                                await self.capture_and_publish_frame(session)
                            elif tipo == "ws_disconnected":
                                logger.info("WebSocket remoto desconectado para handoff %s. Iniciando temporizador de 90s.", session.handoff_id)
                                async def _remote_abandon_timer():
                                    try:
                                        await asyncio.sleep(90)
                                        async with self._lock:
                                            if not session.event_resolved.is_set() and not session.event_cancelled.is_set():
                                                logger.warning("Handoff %s abandonado: 90s sin reconexión. Liberando Chromium.", session.handoff_id)
                                                session.is_abandoned = True
                                                session.event_cancelled.set()
                                    except asyncio.CancelledError:
                                        pass

                                if session.disconnect_timer_task and not session.disconnect_timer_task.done():
                                    session.disconnect_timer_task.cancel()
                                session.disconnect_timer_task = asyncio.create_task(_remote_abandon_timer())
                            else:
                                await self.handle_client_message(session.handoff_id, payload)
                        except Exception as exc:
                            logger.warning("Error procesando mensaje de control remoto para handoff %s: %s", session.handoff_id, exc)
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Error en listener de control remoto para handoff %s: %s", session.handoff_id, exc)
        finally:
            if pubsub:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.aclose()
                except Exception:
                    pass

    async def stop_cdp_screencast(self, session: ActiveHandoffSession) -> None:
        """Detiene el screencast y desconecta CDP. Notifica finalización por Redis stream."""
        if session.periodic_task and not session.periodic_task.done():
            session.periodic_task.cancel()
            session.periodic_task = None

        if session.cdp:
            try:
                await session.cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await session.cdp.detach()
            except Exception:
                pass
            session.cdp = None

        # Notificar fin de sesión a través de Redis y limpiar claves temporales
        try:
            r = get_redis_client()
            status_msg = "done" if session.event_resolved.is_set() else "cancel"
            await r.publish(
                f"handoff:{session.handoff_id}:stream",
                json.dumps({"t": status_msg, "ok": session.event_resolved.is_set()}),
            )
            await r.delete(
                f"handoff:{session.handoff_id}:meta",
                f"handoff:{session.handoff_id}:last_frame",
            )
        except Exception as exc:
            logger.debug("Error limpiando Redis en stop_cdp_screencast: %s", exc)


# Instancia singleton del HandoffManager
_handoff_manager = HandoffManager()


def get_handoff_manager() -> HandoffManager:
    return _handoff_manager
