"use client";

import React, { useEffect, useState } from "react";
import { API_BASE_URL } from "@/lib/api";
import { Check, AlertCircle, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

export interface LogStep {
  id: string | number;
  tipo: string;
  mensaje: string;
  subtext?: string;
  status: "done" | "now" | "warn";
  ts: string;
}

interface TicketStreamLogProps {
  ticketId: string;
  tenantId: string;
  onFinished?: () => void;
  onEsperaHumano?: () => void;
}

export function TicketStreamLog({
  ticketId,
  tenantId,
  onFinished,
  onEsperaHumano,
}: TicketStreamLogProps) {
  const [steps, setSteps] = useState<LogStep[]>([]);
  const [isDone, setIsDone] = useState(false);
  const [isError, setIsError] = useState(false);

  useEffect(() => {
    let isCancelled = false;

    // Conectar vía fetch con credentials: "include" y header X-Tenant-Id
    // para leer el stream SSE sin perder cookies de sesión
    const abortController = new AbortController();

    async function startStream() {
      try {
        const streamUrl = `${API_BASE_URL}/v1/tickets/${ticketId}/stream`;
        const response = await fetch(streamUrl, {
          headers: {
            "Accept": "text/event-stream",
            "X-Tenant-Id": tenantId,
          },
          credentials: "include",
          signal: abortController.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error(`Error al conectar con el stream SSE: ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (!isCancelled) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n\n");
          buffer = lines.pop() || "";

          for (const block of lines) {
            if (!block.trim() || block.startsWith(":")) {
              // Comentario keep-alive o connected
              continue;
            }

            const eventMatch = block.match(/^event:\s*(.+)$/m);
            const dataMatch = block.match(/^data:\s*(.+)$/m);

            const eventType = eventMatch ? eventMatch[1].trim() : "message";
            const dataRaw = dataMatch ? dataMatch[1].trim() : "";

            if (eventType === "close") {
              setIsDone(true);
              if (onFinished) onFinished();
              return;
            }

            if (dataRaw) {
              try {
                const parsed = JSON.parse(dataRaw);
                const stepId = parsed.id || `${Date.now()}-${Math.random()}`;
                const tipo = parsed.tipo || eventType;
                const mensaje = parsed.mensaje || "Paso de procesamiento";
                let subtext = "";

                if (parsed.meta) {
                  if (parsed.meta.error) {
                    const errStr = String(parsed.meta.error);
                    if (
                      errStr.toLowerCase().includes("credit balance is too low") ||
                      errStr.toLowerCase().includes("plans & billing")
                    ) {
                      subtext = "Saldo de Anthropic agotado. Por favor añade créditos en console.anthropic.com";
                    } else if (
                      errStr.toLowerCase().includes("invalid x-api-key") ||
                      errStr.toLowerCase().includes("authentication_error")
                    ) {
                      subtext = "La clave ANTHROPIC_API_KEY en .env es inválida.";
                    } else {
                      subtext = errStr.length > 180 ? `${errStr.slice(0, 180)}...` : errStr;
                    }
                  } else if (parsed.meta.folio) {
                    subtext = `folio ${parsed.meta.folio} · total $${parsed.meta.total || "—"}`;
                  } else if (parsed.meta.file_size) {
                    const kb = Math.round(parsed.meta.file_size / 1024);
                    subtext = `${kb} KB · sesión aislada`;
                  } else if (parsed.meta.modelo_destino) {
                    subtext = "análisis visual HD avanzado";
                  }
                }

                let status: "done" | "now" | "warn" = "now";
                if (tipo === "facturado") {
                  status = "done";
                  setIsDone(true);
                  if (onFinished) onFinished();
                } else if (tipo === "espera_humano" || tipo === "escalamiento_modelo") {
                  status = "warn";
                  if (tipo === "espera_humano" && onEsperaHumano) onEsperaHumano();
                } else if (tipo === "error" || tipo === "rechazado") {
                  status = "warn";
                  setIsDone(true);
                  if (onFinished) onFinished();
                }

                setSteps((prev) => {
                  // Marcar los anteriores como done
                  const updated = prev.map((s) =>
                    s.status === "now" ? { ...s, status: "done" as const } : s
                  );
                  // Verificar si ya existe este paso
                  const exists = updated.some((s) => s.id === stepId);
                  if (exists) return updated;
                  return [
                    ...updated,
                    {
                      id: stepId,
                      tipo,
                      mensaje,
                      subtext,
                      status,
                      ts: parsed.ts || new Date().toISOString(),
                    },
                  ];
                });
              } catch {
                // Fallo de parseo de bloque de datos individual
              }
            }
          }
        }
      } catch (err: unknown) {
        if (!isCancelled && (err as Error).name !== "AbortError") {
          setIsError(true);
        }
      }
    }

    startStream();

    return () => {
      isCancelled = true;
      abortController.abort();
    };
  }, [ticketId, tenantId, onFinished, onEsperaHumano]);

  return (
    <div className="flex flex-col gap-0 py-2">
      {steps.map((step) => {
        const isNow = step.status === "now" && !isDone;
        const isDoneStep = step.status === "done" || (isDone && step.status !== "warn");
        const isWarn = step.status === "warn";

        return (
          <div
            key={step.id}
            className="animate-slide-in flex items-start gap-3 py-2 text-left"
          >
            {/* Indicador circular */}
            <div
              className={cn(
                "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px]",
                isDoneStep && "border-ok bg-ok text-panel",
                isNow && "border-live border-r-transparent animate-spin-custom text-live",
                isWarn && "border-warn bg-warn text-warn-soft font-bold"
              )}
            >
              {isDoneStep && <Check className="h-2.5 w-2.5 stroke-[3]" />}
              {isNow && <Loader2 className="h-2.5 w-2.5 animate-spin text-live" />}
              {isWarn && "!"}
            </div>

            {/* Texto y subtexto mono */}
            <div className="min-w-0 flex-1">
              <span
                className={cn(
                  "text-[13px] font-medium leading-snug",
                  isWarn ? "text-warn font-semibold" : "text-ink"
                )}
              >
                {step.mensaje}
              </span>
              {step.subtext && (
                <span className="mono mt-0.5 block break-words text-[11px] text-muted">
                  {step.subtext}
                </span>
              )}
            </div>
          </div>
        );
      })}

      {steps.length === 0 && !isError && (
        <div className="flex items-center gap-2.5 py-4 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin text-brand" />
          <span>Conectando con el agente de visión...</span>
        </div>
      )}

      {isError && steps.length === 0 && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-bad/30 bg-bad-soft p-3 text-xs text-bad">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>No se pudo conectar en tiempo real con la bitácora del agente. El ticket continúa procesándose en el servidor.</span>
        </div>
      )}
    </div>
  );
}
