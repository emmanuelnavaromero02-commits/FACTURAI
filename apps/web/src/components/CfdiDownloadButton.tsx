"use client";

import React, { useEffect, useState } from "react";
import { Download, Clock, FileText, FileCode, Archive } from "lucide-react";
import { getCfdiDownloadUrl, getCfdiPdfDownloadUrl, getCfdiXmlDownloadUrl } from "@/lib/api";

interface CfdiDownloadButtonProps {
  ticketId: string;
  disponibleHasta?: string | null; // ISO datetime
  tenantId?: string | null;
  compact?: boolean;
}

export function CfdiDownloadButton({
  ticketId,
  disponibleHasta,
  tenantId,
  compact = false,
}: CfdiDownloadButtonProps) {
  const [secondsRemaining, setSecondsRemaining] = useState<number | null>(null);

  useEffect(() => {
    if (!disponibleHasta) {
      setSecondsRemaining(null);
      return;
    }

    function calculateRemaining() {
      const expireTime = new Date(disponibleHasta!).getTime();
      const now = Date.now();
      return Math.max(0, Math.floor((expireTime - now) / 1000));
    }

    setSecondsRemaining(calculateRemaining());

    const interval = setInterval(() => {
      const remaining = calculateRemaining();
      setSecondsRemaining(remaining);
      if (remaining <= 0) {
        clearInterval(interval);
      }
    }, 1000);

    return () => clearInterval(interval);
  }, [disponibleHasta]);

  if (!disponibleHasta) {
    return (
      <span
        className="inline-flex items-center gap-1 text-[11px] text-muted"
        title="La factura fue tramitada y enviada por el portal del emisor a tu correo registrado"
      >
        <span>Enviado al correo</span>
      </span>
    );
  }

  if (secondsRemaining !== null && secondsRemaining <= 0) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md border border-line-2 bg-panel-2 px-2 py-1 text-[11px] text-muted">
        <Clock className="h-3 w-3" />
        <span>CFDI expirado</span>
      </span>
    );
  }

  const mins = secondsRemaining !== null ? Math.floor(secondsRemaining / 60) : 0;
  const secs = secondsRemaining !== null ? secondsRemaining % 60 : 0;
  const timeStr = `${mins}:${secs < 10 ? "0" : ""}${secs}`;

  const pdfUrl = getCfdiPdfDownloadUrl(ticketId, tenantId || undefined);
  const xmlUrl = getCfdiXmlDownloadUrl(ticketId, tenantId || undefined);
  const zipUrl = getCfdiDownloadUrl(ticketId, tenantId || undefined);

  const handleDownload = async (e: React.MouseEvent, url: string, filename: string) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const res = await fetch(url);
      if (!res.ok) {
        if (res.status === 404) {
          alert("El archivo CFDI ha expirado por política de privacidad (30 minutos).");
        } else {
          alert("No se pudo descargar el archivo CFDI.");
        }
        return;
      }
      const blob = await res.blob();
      const objUrl = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(objUrl);
      document.body.removeChild(a);
    } catch (err) {
      console.error("Error al descargar:", err);
      alert("Error de conexión al descargar el comprobante.");
    }
  };

  if (compact) {
    return (
      <div className="inline-flex items-center gap-1">
        <button
          type="button"
          onClick={(e) => handleDownload(e, pdfUrl, `cfdi-${ticketId}.pdf`)}
          className="inline-flex items-center gap-1 rounded bg-red-500/10 hover:bg-red-500/25 text-red-400 border border-red-500/30 px-2 py-0.5 text-xs font-semibold transition active:scale-95"
          title="Ver o descargar PDF"
        >
          <FileText className="h-3 w-3" />
          <span>PDF</span>
        </button>
        <button
          type="button"
          onClick={(e) => handleDownload(e, xmlUrl, `cfdi-${ticketId}.xml`)}
          className="inline-flex items-center gap-1 rounded bg-blue-500/10 hover:bg-blue-500/25 text-blue-400 border border-blue-500/30 px-2 py-0.5 text-xs font-semibold transition active:scale-95"
          title="Descargar XML CFDI 4.0"
        >
          <FileCode className="h-3 w-3" />
          <span>XML</span>
        </button>
        <button
          type="button"
          onClick={(e) => handleDownload(e, zipUrl, `cfdi-${ticketId}.zip`)}
          className="inline-flex items-center gap-1 rounded bg-ok-soft hover:bg-ok/20 text-ok border border-ok/40 px-2 py-0.5 text-xs font-semibold transition active:scale-95"
          title={`Descargar paquete completo ZIP (${timeStr} restantes)`}
        >
          <Download className="h-3 w-3" />
          <span className="mono text-[10px]">{timeStr}</span>
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={(e) => handleDownload(e, pdfUrl, `cfdi-${ticketId}.pdf`)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-red-500/30 bg-red-500/10 px-2.5 py-1 text-xs font-medium text-red-400 hover:bg-red-500/20 transition active:scale-95 shadow-sm"
        title="Ver o descargar comprobante PDF"
      >
        <FileText className="h-3.5 w-3.5" />
        <span>PDF</span>
      </button>

      <button
        type="button"
        onClick={(e) => handleDownload(e, xmlUrl, `cfdi-${ticketId}.xml`)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-blue-500/30 bg-blue-500/10 px-2.5 py-1 text-xs font-medium text-blue-400 hover:bg-blue-500/20 transition active:scale-95 shadow-sm"
        title="Descargar archivo fiscal XML (SAT CFDI 4.0)"
      >
        <FileCode className="h-3.5 w-3.5" />
        <span>XML</span>
      </button>

      <button
        type="button"
        onClick={(e) => handleDownload(e, zipUrl, `cfdi-${ticketId}.zip`)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-ok/40 bg-ok-soft px-3 py-1 text-xs font-semibold text-ok transition hover:bg-ok/20 active:scale-95 shadow-sm"
        title="Descarga efímera ZIP con PDF y XML (válida 30 min)"
      >
        <Archive className="h-3.5 w-3.5 stroke-[2.5]" />
        <span>ZIP</span>
        <span className="mono text-[10px] opacity-75 font-normal">({timeStr})</span>
      </button>
    </div>
  );
}
