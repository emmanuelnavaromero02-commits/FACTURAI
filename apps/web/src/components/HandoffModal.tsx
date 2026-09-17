"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  getTicketHandoffInfo,
  getHandoffWsUrl,
  retryTicket,
  HandoffInfoResponse,
} from "@/lib/api";
import { AlertTriangle, Check, Loader2, RefreshCw, Send, X } from "lucide-react";

interface HandoffModalProps {
  tenantId: string;
  ticketId: string;
  onClose: () => void;
  onSuccess: () => void;
  onRetry?: () => void;
}

export function HandoffModal({
  tenantId,
  ticketId,
  onClose,
  onSuccess,
  onRetry,
}: HandoffModalProps) {
  const [handoffInfo, setHandoffInfo] = useState<HandoffInfoResponse | null>(
    null
  );
  const [loadingInfo, setLoadingInfo] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [wsStatus, setWsStatus] = useState<
    "connecting" | "connected" | "done" | "disconnected" | "error"
  >("connecting");
  const [submissionAttempted, setSubmissionAttempted] = useState(false);
  const [currentFrame, setCurrentFrame] = useState<string | null>(null);
  const [timeLeft, setTimeLeft] = useState<number>(0);
  const [isSubmittingDone, setIsSubmittingDone] = useState(false);
  const [customText, setCustomText] = useState("");
  const [retrying, setRetrying] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const timerRef = useRef<NodeJS.Timeout | null>(null);

  // 1. Cargar información de la sesión de handoff
  useEffect(() => {
    let isMounted = true;

    async function loadInfo() {
      setLoadingInfo(true);
      setErrorMsg(null);
      try {
        const info = await getTicketHandoffInfo(tenantId, ticketId);
        if (!isMounted) return;
        setHandoffInfo(info);

        // Si el backend reporta que ya expiró
        if (info.is_expired) {
          setErrorMsg(
            "Esta sesión de intervención ya finalizó o expiró. Puedes iniciar un nuevo intento."
          );
          setWsStatus("disconnected");
          setTimeLeft(0);
          return;
        }

        // Calcular tiempo restante inicial
        if (info.expires_at) {
          const expiresMs = new Date(info.expires_at).getTime();
          const diffSec = Math.max(
            0,
            Math.floor((expiresMs - Date.now()) / 1000)
          );
          setTimeLeft(diffSec);
        }
      } catch (err: unknown) {
        if (!isMounted) return;
        setErrorMsg(
          "No hay una sesión interactiva activa en este momento para este ticket."
        );
        setWsStatus("disconnected");
        setTimeLeft(0);
      } finally {
        if (isMounted) setLoadingInfo(false);
      }
    }

    loadInfo();

    return () => {
      isMounted = false;
    };
  }, [tenantId, ticketId]);

  // 2. Conectar al WebSocket una vez que tenemos la sesión de handoff
  useEffect(() => {
    if (!handoffInfo) return;
    if (handoffInfo.is_expired) return;

    const wsUrl = getHandoffWsUrl(
      handoffInfo.handoff_id,
      handoffInfo.token,
      handoffInfo.session_token
    );
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setWsStatus("connected");
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.t === "state") {
          if (typeof msg.submission_attempted === "boolean") {
            setSubmissionAttempted(msg.submission_attempted);
          }
        } else if (msg.t === "frame") {
          if (msg.jpeg) {
            setCurrentFrame(msg.jpeg);
          }
        } else if (msg.t === "done") {
          setWsStatus("done");
          setIsSubmittingDone(false);
          onSuccess();
        }
      } catch (err) {
        console.error("Error al procesar mensaje del WebSocket:", err);
      }
    };

    ws.onclose = (event) => {
      if (event.code === 1000 && event.reason === "superseded") {
        setErrorMsg(
          "Esta sesión fue abierta en otra pestaña o ventana del navegador."
        );
      } else if (event.code === 4001) {
        setErrorMsg(event.reason || "Sesión de handoff inválida o expirada.");
      } else if (event.code === 4003) {
        setErrorMsg("Acceso no autorizado al handoff de este ticket.");
      } else if (event.code === 1011) {
        setErrorMsg(
          event.reason ||
            "La sesión no está activa en el navegador del worker o ya concluyó."
        );
      } else if (event.code !== 1000) {
        setErrorMsg(event.reason || `Conexión cerrada (${event.code}).`);
      }
      setWsStatus("disconnected");
    };

    ws.onerror = () => {
      setWsStatus("error");
    };

    return () => {
      if (
        ws.readyState === WebSocket.OPEN ||
        ws.readyState === WebSocket.CONNECTING
      ) {
        ws.close();
      }
      wsRef.current = null;
    };
  }, [handoffInfo, onSuccess]);

  // 3. Temporizador de cuenta regresiva
  useEffect(() => {
    timerRef.current = setInterval(() => {
      setTimeLeft((prev) => {
        if (prev <= 1) {
          if (timerRef.current) clearInterval(timerRef.current);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  // Formato MM:SS
  const formatTime = (secs: number) => {
    const m = Math.floor(secs / 60)
      .toString()
      .padStart(2, "0");
    const s = (secs % 60).toString().padStart(2, "0");
    return `${m}:${s}`;
  };

  // Enviar clic normalizado
  const handleImageClick = (e: React.MouseEvent<HTMLImageElement>) => {
    if (
      !imgRef.current ||
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN
    )
      return;
    const rect = imgRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    wsRef.current.send(JSON.stringify({ t: "click", x, y }));
  };

  // Enviar toque en pantallas móviles
  const handleImageTouch = (e: React.TouchEvent<HTMLImageElement>) => {
    if (
      !imgRef.current ||
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN
    )
      return;
    if (e.touches.length === 0) return;
    const touch = e.touches[0];
    const rect = imgRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (touch.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (touch.clientY - rect.top) / rect.height));
    wsRef.current.send(JSON.stringify({ t: "click", x, y }));
  };

  // Enviar texto manual si el captcha requiere escritura
  const handleSendText = (e: React.FormEvent) => {
    e.preventDefault();
    if (
      !customText.trim() ||
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN
    )
      return;
    wsRef.current.send(JSON.stringify({ t: "text", v: customText }));
    setCustomText("");
  };

  // Reintentar ticket y cerrar modal
  const handleRetryTicket = async () => {
    setRetrying(true);
    try {
      await retryTicket(tenantId, ticketId);
      if (onRetry) onRetry();
      onClose();
    } catch (err: unknown) {
      setErrorMsg((err as Error).message || "No se pudo reintentar el ticket.");
    } finally {
      setRetrying(false);
    }
  };

  // Botón "Lo hago después" / Cancelar
  const handleCancel = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ t: "cancel" }));
      wsRef.current.close();
    }
    onClose();
  };

  // Botón "Listo, continuar"
  const handleDone = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      setIsSubmittingDone(true);
      wsRef.current.send(JSON.stringify({ t: "done" }));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 sm:p-4 backdrop-blur-xs">
      <div className="flex max-h-[94vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-line bg-panel shadow-2xl">
        {/* Cabecera del modal */}
        <div className="flex items-start justify-between border-b border-line px-5 py-4">
          <div>
            <h3 className="text-base font-bold text-ink">
              El portal pide verificación
            </h3>
            <p className="mt-0.5 text-xs text-muted">
              La IA llenó todo. Solo falta que tú marques la casilla — después
              continúa sola.
            </p>
          </div>
          <button
            type="button"
            onClick={handleCancel}
            className="flex h-7 w-7 items-center justify-center rounded-lg bg-panel-2 text-muted transition-colors hover:text-ink"
            aria-label="Cerrar modal"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Cuerpo del modal */}
        <div className="flex flex-1 flex-col overflow-y-auto p-4 sm:p-5">
          {/* Advertencia de irreversibilidad si el agente ya ejecutó el envío final */}
          {submissionAttempted && (
            <div className="mb-3 flex items-start gap-2.5 rounded-xl border border-bad/30 bg-bad-soft p-3 text-xs text-bad">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <b className="font-bold">Advertencia importante:</b>
                <p className="mt-0.5">
                  Esta factura ya se envió, no vuelvas a oprimir el botón de
                  facturar.
                </p>
              </div>
            </div>
          )}

          {errorMsg && wsStatus === "error" && (
            <div className="mb-3 rounded-xl border border-bad/30 bg-bad-soft p-3 text-xs text-bad">
              {errorMsg}
            </div>
          )}

          {/* Marco simulado de navegador (Browser Frame) */}
          <div className="overflow-hidden rounded-xl border border-line-2 bg-panel-2">
            {/* Barra superior de URL */}
            <div className="flex items-center gap-2.5 border-b border-line-2 bg-panel-3 px-3 py-2">
              <div className="flex gap-1.5">
                <span className="h-2.5 w-2.5 rounded-full bg-line-2" />
                <span className="h-2.5 w-2.5 rounded-full bg-line-2" />
                <span className="h-2.5 w-2.5 rounded-full bg-line-2" />
              </div>

              <div className="mono flex-1 truncate rounded-md border border-line bg-panel px-2.5 py-1 text-xs text-ink-2">
                🔒 {handoffInfo?.url_facturacion || "portal de facturación"}
              </div>

              <div>
                {wsStatus === "connected" ? (
                  <span className="flex items-center gap-1.5 rounded-full bg-live-soft px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-live">
                    <span className="h-1.5 w-1.5 rounded-full bg-live animate-pulse-fast" />
                    En vivo
                  </span>
                ) : wsStatus === "connecting" ? (
                  <span className="flex items-center gap-1.5 rounded-full bg-warn-soft px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-warn">
                    <Loader2 className="h-2.5 w-2.5 animate-spin-custom" />
                    Conectando
                  </span>
                ) : (
                  <span className="rounded-full bg-panel px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-muted">
                    Desconectado
                  </span>
                )}
              </div>
            </div>

            {/* Pantalla interactiva con Streaming CDP */}
            <div className="relative flex aspect-[16/10] w-full items-center justify-center overflow-hidden bg-panel-2">
              {currentFrame ? (
                <img
                  ref={imgRef}
                  src={`data:image/jpeg;base64,${currentFrame}`}
                  alt="Navegador del portal de facturación"
                  className="h-full w-full cursor-crosshair select-none object-contain"
                  onClick={handleImageClick}
                  onTouchStart={handleImageTouch}
                  draggable={false}
                />
              ) : wsStatus === "disconnected" || wsStatus === "error" || handoffInfo?.is_expired ? (
                <div className="flex flex-col items-center gap-3 p-6 text-center max-w-md">
                  <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-warn-soft text-warn">
                    <AlertTriangle className="h-6 w-6" />
                  </div>
                  <div>
                    <h4 className="text-sm font-bold text-ink">Sesión no disponible o finalizada</h4>
                    <p className="mt-1 text-xs text-muted">
                      {errorMsg || "El navegador remoto liberó la sesión por límite de tiempo o el ticket volvió a la cola de reintentos."}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={handleRetryTicket}
                    disabled={retrying}
                    className="mt-2 flex items-center gap-2 rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand shadow hover:brightness-105 transition active:scale-95 disabled:opacity-50"
                  >
                    <RefreshCw className={`h-3.5 w-3.5 ${retrying ? "animate-spin" : ""}`} />
                    <span>Reintentar facturación para abrir navegador</span>
                  </button>
                </div>
              ) : (
                <div className="flex flex-col items-center gap-2 p-6 text-center text-muted">
                  <Loader2 className="h-8 w-8 animate-spin-custom text-brand" />
                  <p className="text-xs font-medium">
                    {loadingInfo
                      ? "Cargando sesión..."
                      : "Esperando señal del navegador remoto..."}
                  </p>
                  <p className="text-[11px]">
                    El portal remoto está cargando. En cuanto aparezca el desafío o formulario, podrás interactuar directamente haciendo clic en la pantalla.
                  </p>
                </div>
              )}
            </div>

            {/* Barra auxiliar para capturas de texto si el captcha requiere escribir caracteres */}
            {wsStatus === "connected" && (
              <form
                onSubmit={handleSendText}
                className="flex items-center gap-2 border-t border-line-2 bg-panel px-3 py-1.5"
              >
                <span className="text-[11px] text-muted">
                  ¿El captcha pide texto?
                </span>
                <input
                  type="text"
                  value={customText}
                  onChange={(e) => setCustomText(e.target.value)}
                  placeholder="Escribe el código aquí..."
                  className="flex-1 rounded-lg border border-line-2 bg-panel-2 px-2.5 py-1 text-xs text-ink placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-brand"
                />
                <button
                  type="submit"
                  disabled={!customText.trim()}
                  className="flex items-center gap-1 rounded-lg bg-brand px-2.5 py-1 text-xs font-medium text-on-brand disabled:opacity-40"
                >
                  <Send className="h-3 w-3" />
                  <span>Enviar</span>
                </button>
              </form>
            )}
          </div>

          {/* Temporizador */}
          <div className="mt-3 text-center text-xs text-muted">
            {timeLeft > 0 ? (
              <p>
                La sesión se libera en{" "}
                <b className="mono font-bold text-warn">
                  {formatTime(timeLeft)}
                </b>{" "}
                · después el ticket vuelve a la cola
              </p>
            ) : wsStatus === "connected" ? (
              <p className="font-semibold text-bad">
                La sesión expiró. El ticket volvió a la cola de reintentos.
              </p>
            ) : (
              <p className="text-muted">
                Sesión inactiva · Puedes reintentar la facturación para abrir un nuevo navegador.
              </p>
            )}
          </div>
        </div>

        {/* Pie del modal */}
        <div className="flex flex-wrap items-center justify-end gap-2.5 border-t border-line bg-panel-2 px-5 py-3">
          <button
            type="button"
            onClick={handleCancel}
            className="rounded-xl border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink-2 transition-colors hover:bg-line/20"
          >
            {wsStatus === "disconnected" || wsStatus === "error" || handoffInfo?.is_expired ? "Cerrar" : "Lo hago después"}
          </button>

          {wsStatus === "disconnected" || wsStatus === "error" || handoffInfo?.is_expired ? (
            <button
              type="button"
              onClick={handleRetryTicket}
              disabled={retrying}
              className="flex items-center gap-1.5 rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand shadow-sm transition-all hover:brightness-105 active:scale-95 disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${retrying ? "animate-spin" : ""}`} />
              <span>Reintentar facturación</span>
            </button>
          ) : (
            <button
              type="button"
              onClick={handleDone}
              disabled={
                wsStatus !== "connected" || isSubmittingDone || timeLeft <= 0
              }
              className="flex items-center gap-1.5 rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand shadow-sm transition-all hover:brightness-105 active:scale-95 disabled:opacity-50"
            >
              {isSubmittingDone ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin-custom" />
                  <span>Reanudando agente...</span>
                </>
              ) : (
                <>
                  <Check className="h-3.5 w-3.5 stroke-[2.5]" />
                  <span>Listo, continuar</span>
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
