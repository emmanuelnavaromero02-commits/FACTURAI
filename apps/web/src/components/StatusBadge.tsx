import React from "react";
import { cn } from "@/lib/utils";

interface StatusBadgeProps {
  estado: string;
  errorCode?: string | null;
  errorMsg?: string | null;
  className?: string;
}

export function StatusBadge({ estado, errorCode, errorMsg, className }: StatusBadgeProps) {
  let label = estado;
  let styleClass = "text-muted bg-panel-2 border-line-2";
  let showPulse = false;

  switch (estado?.toLowerCase()) {
    case "facturado":
      label = "Facturado";
      styleClass = "text-ok bg-ok-soft border-ok/40";
      break;
    case "facturando":
      label = "Facturando";
      styleClass = "text-live bg-live-soft border-live/40";
      showPulse = true;
      break;
    case "extrayendo":
      label = "Extrayendo";
      styleClass = "text-live bg-live-soft border-live/40";
      showPulse = true;
      break;
    case "espera_humano":
      label = "Espera humano";
      styleClass = "text-warn bg-warn-soft border-warn/45";
      break;
    case "encolado":
      label = "En cola";
      styleClass = "text-muted bg-panel-2 border-line-2";
      break;
    case "recibido":
      label = "Recibido";
      styleClass = "text-muted bg-panel-2 border-line-2";
      break;
    case "extraido":
      label = "Extraído";
      styleClass = "text-live bg-live-soft border-line-2";
      break;
    case "rechazado":
      if (errorCode === "ticket_ya_facturado" || errorCode === "duplicado") {
        label = "Ya facturado";
      } else if (errorCode === "perfil_incompleto") {
        label = "Datos faltantes";
      } else if (errorCode === "imagen_ilegible") {
        label = "Foto ilegible";
      } else if (errorCode === "handoff_expirado" || errorCode === "tiempo_expirado") {
        label = "Tiempo expirado";
      } else if (errorCode === "ticket_vencido") {
        label = "Ticket vencido";
      } else if (errorCode === "datos_no_coinciden") {
        label = "Monto no coincide";
      } else if (errorCode === "folio_no_encontrado") {
        label = "Folio no hallado";
      } else if (errorCode === "folio_invalido") {
        label = "Folio no válido";
      } else {
        label = "Rechazado";
      }
      styleClass = "text-bad bg-bad-soft border-bad/40";
      break;
    case "cancelado":
      label = "Cancelado";
      styleClass = "text-bad bg-bad-soft border-bad/40";
      break;
    default:
      label = estado || "Desconocido";
  }

  return (
    <span
      title={errorMsg || label}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider",
        styleClass,
        className
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full bg-current",
          showPulse && "animate-pulse-fast"
        )}
      />
      {label}
    </span>
  );
}
