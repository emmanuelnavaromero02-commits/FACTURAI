"use client";

import React, { useState } from "react";
import {
  X,
  FileText,
  FileCode,
  Archive,
  ExternalLink,
  RefreshCw,
  Play,
  CheckCircle2,
  AlertCircle,
  Clock,
  Building2,
  Copy,
  Check,
  Zap,
  Mail,
  Loader2,
} from "lucide-react";
import { TicketResponse } from "@/types/api";
import { StatusBadge } from "@/components/StatusBadge";
import { formatCurrency } from "@/lib/utils";
import { getCfdiDownloadUrl, getCfdiPdfDownloadUrl, getCfdiXmlDownloadUrl } from "@/lib/api";

interface TicketDetailModalProps {
  ticket: TicketResponse;
  tenantId?: string | null;
  onClose: () => void;
  onFacturar: (id: string) => void;
  onRetry: (id: string) => void;
  onOpenHandoff?: (ticket: TicketResponse) => void;
}

export function TicketDetailModal({
  ticket,
  tenantId,
  onClose,
  onFacturar,
  onRetry,
  onOpenHandoff,
}: TicketDetailModalProps) {
  const [copiedUuid, setCopiedUuid] = useState(false);

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedUuid(true);
    setTimeout(() => setCopiedUuid(false), 2000);
  };

  const isFacturado = ticket.estado === "facturado";
  const isEsperaHumano = ticket.estado === "espera_humano";
  const isRechazado = ticket.estado === "rechazado" || ticket.estado === "cancelado";
  const isExtraido = ticket.estado === "extraido";

  const hasActiveCfdi = Boolean(
    ticket.cfdi_disponible_hasta &&
      new Date(ticket.cfdi_disponible_hasta).getTime() > Date.now()
  );

  const [downloading, setDownloading] = useState<string | null>(null);

  const safeDownload = async (url: string, filename: string, type: string) => {
    try {
      setDownloading(type);
      const res = await fetch(url);
      if (!res.ok) {
        if (res.status === 404) {
          alert("El archivo CFDI ya no está disponible en la memoria efímera (expira a los 30 min por privacidad).");
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
      console.error("Error al descargar archivo CFDI:", err);
      alert("Error de conexión al descargar el comprobante fiscal.");
    } finally {
      setDownloading(null);
    }
  };

  const pdfUrl = getCfdiPdfDownloadUrl(ticket.id, tenantId || undefined);
  const xmlUrl = getCfdiXmlDownloadUrl(ticket.id, tenantId || undefined);
  const zipUrl = getCfdiDownloadUrl(ticket.id, tenantId || undefined);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs animate-in fade-in duration-200">
      <div className="relative flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-line bg-panel shadow-2xl">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-line bg-panel-2/50 px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand/10 text-brand">
              <Building2 className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-base font-bold text-ink">
                  {ticket.sucursal || ticket.rfc_emisor || "Detalle del Ticket"}
                </h3>
                <StatusBadge
                  estado={ticket.estado}
                  errorCode={ticket.error_code}
                  errorMsg={ticket.error_msg}
                />
              </div>
              <p className="text-xs text-muted">
                RFC: <span className="mono font-semibold text-ink-2">{ticket.rfc_emisor || "No detectado"}</span>
                {ticket.fecha_ticket && ` · Fecha de compra: ${ticket.fecha_ticket}`}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg bg-panel-2 text-muted hover:text-ink transition"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Scrollable Content */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* Bloque de Estado si ya está Facturado */}
          {isFacturado && (
            <div className="rounded-xl border border-ok/30 bg-ok-soft/30 p-4 shadow-sm">
              <div className="flex items-start justify-between gap-2 mb-3">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="h-5 w-5 text-ok shrink-0" />
                  <div>
                    <h4 className="text-sm font-bold text-ink">Facturación completada con éxito</h4>
                    <p className="text-xs text-muted">
                      {hasActiveCfdi
                        ? "El comprobante fiscal CFDI 4.0 está listo para descarga cifrada inmediata."
                        : "El comprobante fiscal fue timbrado exitosamente ante el SAT."}
                    </p>
                  </div>
                </div>
              </div>

              {/* CFDI UUID */}
              {ticket.cfdi_uuid && (
                <div className="mb-3 flex items-center justify-between rounded-lg border border-line-2 bg-panel px-3 py-2 text-xs">
                  <span className="text-muted">Folio Fiscal (UUID SAT):</span>
                  <div className="flex items-center gap-2">
                    <span className="mono font-semibold text-ink">{ticket.cfdi_uuid}</span>
                    <button
                      type="button"
                      onClick={() => copyToClipboard(ticket.cfdi_uuid!)}
                      className="text-muted hover:text-ink transition"
                      title="Copiar UUID"
                    >
                      {copiedUuid ? <Check className="h-3.5 w-3.5 text-ok" /> : <Copy className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                </div>
              )}

              {/* Descargas activas o Aviso de entrega por Correo */}
              {hasActiveCfdi ? (
                <>
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <button
                      type="button"
                      disabled={downloading !== null}
                      onClick={() => safeDownload(pdfUrl, `cfdi-${ticket.cfdi_uuid || ticket.folio || ticket.id}.pdf`, "pdf")}
                      className="flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2 text-xs font-semibold text-red-400 hover:bg-red-500/20 transition active:scale-95 shadow-sm disabled:opacity-50"
                    >
                      {downloading === "pdf" ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                      <span>Descargar PDF</span>
                    </button>

                    <button
                      type="button"
                      disabled={downloading !== null}
                      onClick={() => safeDownload(xmlUrl, `cfdi-${ticket.cfdi_uuid || ticket.folio || ticket.id}.xml`, "xml")}
                      className="flex items-center gap-2 rounded-xl border border-blue-500/30 bg-blue-500/10 px-4 py-2 text-xs font-semibold text-blue-400 hover:bg-blue-500/20 transition active:scale-95 shadow-sm disabled:opacity-50"
                    >
                      {downloading === "xml" ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileCode className="h-4 w-4" />}
                      <span>Descargar XML</span>
                    </button>

                    <button
                      type="button"
                      disabled={downloading !== null}
                      onClick={() => safeDownload(zipUrl, `cfdi-${ticket.cfdi_uuid || ticket.folio || ticket.id}.zip`, "zip")}
                      className="flex items-center gap-2 rounded-xl border border-ok/40 bg-ok-soft px-4 py-2 text-xs font-bold text-ok hover:bg-ok/25 transition active:scale-95 shadow-sm disabled:opacity-50"
                    >
                      {downloading === "zip" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Archive className="h-4 w-4 stroke-[2.5]" />}
                      <span>Paquete Completo (ZIP)</span>
                    </button>
                  </div>

                  <p className="mt-2.5 flex items-center gap-1.5 text-[11px] text-muted">
                    <Clock className="h-3.5 w-3.5 text-ok" />
                    <span>
                      Descargas efímeras con cifrado AES-256 de extremo a extremo (disponibles por 30 min).
                    </span>
                  </p>
                </>
              ) : (
                <div className="rounded-xl border border-line-2 bg-panel p-3.5 text-xs">
                  <div className="flex items-center gap-2 text-ink font-semibold">
                    <Mail className="h-4 w-4 text-brand" />
                    <span>Factura entregada por correo electrónico</span>
                  </div>
                  <p className="mt-1 text-[11px] text-muted">
                    {ticket.correo_capturado_en_portal
                      ? `El portal del comercio timbró la factura y envió los archivos XML/PDF a: ${ticket.correo_capturado_en_portal}`
                      : "El emisor envió la factura a tu correo registrado o el periodo de descarga efímera ha expirado."}
                  </p>
                  <p className="mt-2 text-[10px] text-muted/70">
                    FacturAI opera con Cero Retención en disco para garantizar la máxima privacidad fiscal.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Bloque si Requiere Humano */}
          {isEsperaHumano && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
              <div className="flex items-start gap-3">
                <AlertCircle className="h-5 w-5 text-amber-500 shrink-0 mt-0.5" />
                <div className="flex-1">
                  <h4 className="text-sm font-bold text-ink">Se requiere intervención humana</h4>
                  <p className="text-xs text-muted mt-0.5">
                    El portal del comercio solicitó resolver un captcha o verificar un dato especial.
                  </p>
                  {onOpenHandoff && (
                    <button
                      type="button"
                      onClick={() => onOpenHandoff(ticket)}
                      className="mt-3 flex items-center gap-2 rounded-xl bg-amber-500 px-4 py-2 text-xs font-bold text-white shadow hover:brightness-105 active:scale-95"
                    >
                      <Zap className="h-4 w-4" />
                      <span>Tomar control en vivo (Handoff)</span>
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Bloque si tiene Error / Rechazado */}
          {isRechazado && (
            <div className="rounded-xl border border-bad/30 bg-bad/10 p-4">
              <div className="flex items-start gap-3">
                <AlertCircle className="h-5 w-5 text-bad shrink-0 mt-0.5" />
                <div className="flex-1">
                  <h4 className="text-sm font-bold text-ink">Error en la facturación</h4>
                  <p className="text-xs text-bad/90 mt-1 font-mono">
                    {ticket.error_msg || ticket.error_code || "El portal no completó la emisión."}
                  </p>
                  <button
                    type="button"
                    onClick={() => onRetry(ticket.id)}
                    className="mt-3 flex items-center gap-2 rounded-xl border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink hover:bg-panel-2 active:scale-95 transition"
                  >
                    <RefreshCw className="h-4 w-4" />
                    <span>Reintentar facturación</span>
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Datos Fiscales y Montos */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted mb-3">
              Datos Extraídos del Comprobante
            </h4>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Total</span>
                <p className="mono text-lg font-bold text-ink mt-0.5">
                  {formatCurrency(ticket.total)}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Subtotal</span>
                <p className="mono text-sm font-semibold text-ink mt-1">
                  {ticket.subtotal ? formatCurrency(ticket.subtotal) : "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">IVA</span>
                <p className="mono text-sm font-semibold text-ink mt-1">
                  {ticket.iva ? formatCurrency(ticket.iva) : "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Folio / Ticket</span>
                <p className="mono text-sm font-semibold text-ink mt-1">
                  {ticket.folio || "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Sucursal / Tienda</span>
                <p className="text-xs font-semibold text-ink mt-1 truncate" title={ticket.sucursal || ""}>
                  {ticket.sucursal || "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Caja / Transacción</span>
                <p className="mono text-xs font-semibold text-ink mt-1">
                  {ticket.caja || ticket.web_id || "—"}
                </p>
              </div>
            </div>
          </div>

          {/* Portal de Facturación Investigado */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted mb-2">
              Portal de Facturación Investigado
            </h4>
            <div className="flex items-center justify-between rounded-xl border border-line bg-panel-2/50 p-3">
              <div className="min-w-0 flex-1 pr-2">
                <p className="text-xs text-muted">URL del portal:</p>
                {ticket.url_facturacion ? (
                  <a
                    href={ticket.url_facturacion}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center gap-1.5 text-xs font-medium text-brand hover:underline mt-0.5 truncate"
                  >
                    <span className="truncate">{ticket.url_facturacion}</span>
                    <ExternalLink className="h-3 w-3 shrink-0" />
                  </a>
                ) : (
                  <p className="text-xs text-muted italic mt-0.5">
                    Investigación automática en curso o determinada al facturar.
                  </p>
                )}
              </div>
            </div>
          </div>

          {/* Métricas y Auditoría */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted mb-2">
              Información de Procesamiento y Privacidad
            </h4>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 text-xs">
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Intentos</span>
                <p className="mono font-semibold text-ink">{ticket.intentos} / 3</p>
              </div>
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Pasos Agente</span>
                <p className="mono font-semibold text-ink">{ticket.pasos_agente || 1}</p>
              </div>
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Duración</span>
                <p className="mono font-semibold text-ink">
                  {ticket.duracion_segundos ? `${Math.round(ticket.duracion_segundos)}s` : "—"}
                </p>
              </div>
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Imagen Original</span>
                <p className="font-semibold text-ok">
                  {!ticket.image_key || isFacturado ? "✓ Eliminada" : "En proceso"}
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Footer Actions */}
        <div className="flex items-center justify-between border-t border-line bg-panel-2/50 px-6 py-3">
          <span className="text-[11px] text-muted">
            ID: <span className="mono">{ticket.id.slice(0, 8)}</span>
          </span>
          <div className="flex items-center gap-2">
            {isExtraido && (
              <button
                type="button"
                onClick={() => {
                  onFacturar(ticket.id);
                  onClose();
                }}
                className="flex items-center gap-1.5 rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand shadow hover:brightness-105"
              >
                <Play className="h-3.5 w-3.5 fill-current" />
                <span>Facturar ahora</span>
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="rounded-xl border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink hover:bg-panel-2 transition"
            >
              Cerrar
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
