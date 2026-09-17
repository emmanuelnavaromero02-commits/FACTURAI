"use client";

import React, { useState, useEffect } from "react";
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
  ShieldCheck,
  ShieldAlert,
  Fuel,
  Hotel,
  Utensils,
  ShoppingCart,
  Navigation,
  Plane,
  Fingerprint,
  Globe,
  Sparkles,
  Edit3,
  Save,
  Camera,
  Maximize2,
  Eye,
  EyeOff,
} from "lucide-react";
import { TicketResponse } from "@/types/api";
import { StatusBadge } from "@/components/StatusBadge";
import { formatCurrency } from "@/lib/utils";
import {
  API_BASE_URL,
  getCfdiDownloadUrl,
  getCfdiPdfDownloadUrl,
  getCfdiXmlDownloadUrl,
  updateTicketPortal,
  deduceTicketPortal,
} from "@/lib/api";

interface TicketDetailModalProps {
  ticket: TicketResponse;
  tenantId?: string | null;
  onClose: () => void;
  onFacturar: (id: string) => void;
  onRetry: (id: string) => void;
  onOpenHandoff?: (ticket: TicketResponse) => void;
  onTicketUpdated?: (ticket: TicketResponse) => void;
}

export function TicketDetailModal({
  ticket,
  tenantId,
  onClose,
  onFacturar,
  onRetry,
  onOpenHandoff,
  onTicketUpdated,
}: TicketDetailModalProps) {
  const [currentTicket, setCurrentTicket] = useState<TicketResponse>(ticket);
  const [copiedUuid, setCopiedUuid] = useState(false);
  const [copiedHash, setCopiedHash] = useState(false);

  // Estados para investigación y edición de portal
  const [isEditingPortal, setIsEditingPortal] = useState<boolean>(
    !ticket.url_facturacion &&
      (ticket.estado === "extraido" ||
        ticket.estado === "espera_humano" ||
        ticket.estado === "rechazado")
  );
  const [portalInput, setPortalInput] = useState<string>(ticket.url_facturacion || "");
  const [isSearchingPortal, setIsSearchingPortal] = useState<boolean>(false);
  const [isSavingPortal, setIsSavingPortal] = useState<boolean>(false);
  const [portalCandidates, setPortalCandidates] = useState<string[]>([]);
  const [portalFeedback, setPortalFeedback] = useState<{
    type: "success" | "error" | "info";
    text: string;
  } | null>(null);

  // Estados del Visor de Ticket Original
  const [showImagePreview, setShowImagePreview] = useState<boolean>(true);
  const [isZoomedImage, setIsZoomedImage] = useState<boolean>(false);
  const [imageError, setImageError] = useState<boolean>(false);

  useEffect(() => {
    setCurrentTicket(ticket);
    setPortalInput(ticket.url_facturacion || "");
    setIsEditingPortal(
      !ticket.url_facturacion &&
        (ticket.estado === "extraido" ||
          ticket.estado === "espera_humano" ||
          ticket.estado === "rechazado")
    );
  }, [ticket]);

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedUuid(true);
    setTimeout(() => setCopiedUuid(false), 2000);
  };

  const isFacturado = currentTicket.estado === "facturado";
  const isEsperaHumano = currentTicket.estado === "espera_humano";
  const isRechazado = currentTicket.estado === "rechazado" || currentTicket.estado === "cancelado";
  const isExtraido = currentTicket.estado === "extraido";
  const isMissingPortal =
    !currentTicket.url_facturacion ||
    currentTicket.error_code === "esperando_url_portal" ||
    currentTicket.error_code === "portal_requerido";

  const hasActiveCfdi = Boolean(
    currentTicket.cfdi_disponible_hasta &&
      new Date(currentTicket.cfdi_disponible_hasta).getTime() > Date.now()
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

  const handleDeducePortal = async () => {
    if (!tenantId) return;
    setIsSearchingPortal(true);
    setPortalFeedback(null);
    try {
      const res = await deduceTicketPortal(tenantId, currentTicket.id);
      if (res.candidates && res.candidates.length > 0) {
        setPortalCandidates(res.candidates);
        const best = res.deduced_url || res.candidates[0];
        setPortalInput(best);
        setPortalFeedback({
          type: "success",
          text: `Se encontraron ${res.candidates.length} candidato(s). Sugerencia seleccionada.`,
        });
      } else {
        setPortalFeedback({
          type: "info",
          text: "No se identificó el portal de este comercio en la red de emisores conocidos. Por favor escribe o pega la URL directamente.",
        });
      }
    } catch (err: any) {
      console.error("Error al deducir portal:", err);
      setPortalFeedback({
        type: "error",
        text: err?.message || "Error al investigar el portal con IA.",
      });
    } finally {
      setIsSearchingPortal(false);
    }
  };

  const handleSavePortal = async (facturarAhora: boolean = false) => {
    if (!tenantId) return;
    const cleanUrl = portalInput.trim();
    if (!cleanUrl) {
      setPortalFeedback({
        type: "error",
        text: "Por favor ingresa una URL válida (ej. https://facturacion.comercio.mx).",
      });
      return;
    }

    setIsSavingPortal(true);
    setPortalFeedback(null);
    try {
      const updated = await updateTicketPortal(tenantId, currentTicket.id, cleanUrl, facturarAhora);
      setCurrentTicket(updated);
      setIsEditingPortal(false);
      setPortalCandidates([]);
      if (onTicketUpdated) {
        onTicketUpdated(updated);
      }
      if (facturarAhora) {
        onClose();
      } else {
        setPortalFeedback({
          type: "success",
          text: "Portal guardado con éxito.",
        });
      }
    } catch (err: any) {
      console.error("Error al guardar portal:", err);
      setPortalFeedback({
        type: "error",
        text: err?.message || "No se pudo actualizar el portal del ticket.",
      });
    } finally {
      setIsSavingPortal(false);
    }
  };

  const pdfUrl = getCfdiPdfDownloadUrl(currentTicket.id, tenantId || undefined);
  const xmlUrl = getCfdiXmlDownloadUrl(currentTicket.id, tenantId || undefined);
  const zipUrl = getCfdiDownloadUrl(currentTicket.id, tenantId || undefined);

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
                  {currentTicket.sucursal || currentTicket.rfc_emisor || "Detalle del Ticket"}
                </h3>
                <StatusBadge
                  estado={currentTicket.estado}
                  errorCode={currentTicket.error_code}
                  errorMsg={currentTicket.error_msg}
                />
              </div>
              <p className="text-xs text-muted">
                RFC: <span className="mono font-semibold text-ink-2">{currentTicket.rfc_emisor || "No detectado"}</span>
                {currentTicket.fecha_ticket && ` · Fecha de compra: ${currentTicket.fecha_ticket}`}
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
          {/* Visor de Ticket Original */}
          {currentTicket.image_key && !currentTicket.image_deleted_at && !imageError && (
            <div className="rounded-2xl border border-line bg-panel-2/40 overflow-hidden shadow-xs">
              <div className="flex items-center justify-between border-b border-line px-4 py-2.5 bg-panel-2/60">
                <div className="flex items-center gap-2">
                  <Camera className="h-4 w-4 text-brand" />
                  <span className="text-xs font-bold text-ink">Visor del Ticket Capturado</span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setIsZoomedImage(true)}
                    className="flex items-center gap-1 text-[11px] font-semibold text-brand hover:underline"
                  >
                    <Maximize2 className="h-3 w-3" />
                    <span>Ampliar</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowImagePreview(!showImagePreview)}
                    className="rounded p-1 text-muted hover:text-ink transition"
                    title={showImagePreview ? "Ocultar imagen" : "Mostrar imagen"}
                  >
                    {showImagePreview ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>

              {showImagePreview && (
                <div className="relative flex justify-center bg-black/5 p-3">
                  <div
                    onClick={() => setIsZoomedImage(true)}
                    className="group relative max-h-64 cursor-zoom-in overflow-hidden rounded-lg border border-line/60 shadow-xs transition hover:opacity-95"
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={`${API_BASE_URL}/v1/tickets/${currentTicket.id}/image${tenantId ? `?tenant_id=${tenantId}` : ""}`}
                      alt="Foto original del comprobante"
                      onError={() => setImageError(true)}
                      className="max-h-64 object-contain"
                    />
                    <div className="absolute inset-0 flex items-center justify-center bg-black/20 opacity-0 group-hover:opacity-100 transition">
                      <span className="rounded-full bg-black/70 px-3 py-1 text-[11px] font-semibold text-white backdrop-blur-xs flex items-center gap-1.5">
                        <Maximize2 className="h-3 w-3" /> Clic para ampliar
                      </span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Modal de Zoom de Imagen */}
          {isZoomedImage && (
            <div
              className="fixed inset-0 z-60 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm animate-in fade-in"
              onClick={() => setIsZoomedImage(false)}
            >
              <div
                className="relative max-h-[92vh] max-w-4xl overflow-auto rounded-2xl bg-panel p-2 shadow-2xl"
                onClick={(e) => e.stopPropagation()}
              >
                <div className="flex items-center justify-between border-b border-line px-3 py-2">
                  <span className="text-xs font-bold text-ink flex items-center gap-1.5">
                    <Camera className="h-3.5 w-3.5 text-brand" /> Foto del ticket ({currentTicket.folio || currentTicket.id.slice(0, 8)})
                  </span>
                  <div className="flex items-center gap-2">
                    <a
                      href={`${API_BASE_URL}/v1/tickets/${currentTicket.id}/image${tenantId ? `?tenant_id=${tenantId}` : ""}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-xs text-muted hover:text-ink"
                    >
                      <ExternalLink className="h-3 w-3" /> Abrir pestaña
                    </a>
                    <button
                      type="button"
                      onClick={() => setIsZoomedImage(false)}
                      className="rounded-lg p-1 text-muted hover:text-ink"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <div className="flex justify-center p-2">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={`${API_BASE_URL}/v1/tickets/${currentTicket.id}/image${tenantId ? `?tenant_id=${tenantId}` : ""}`}
                    alt="Ticket ampliado"
                    className="max-h-[80vh] w-auto object-contain rounded-lg"
                  />
                </div>
              </div>
            </div>
          )}
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
              {currentTicket.cfdi_uuid && (
                <div className="mb-3 flex items-center justify-between rounded-lg border border-line-2 bg-panel px-3 py-2 text-xs">
                  <span className="text-muted">Folio Fiscal (UUID SAT):</span>
                  <div className="flex items-center gap-2">
                    <span className="mono font-semibold text-ink">{currentTicket.cfdi_uuid}</span>
                    <button
                      type="button"
                      onClick={() => copyToClipboard(currentTicket.cfdi_uuid!)}
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
                      onClick={() => safeDownload(pdfUrl, `cfdi-${currentTicket.cfdi_uuid || currentTicket.folio || currentTicket.id}.pdf`, "pdf")}
                      className="flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2 text-xs font-semibold text-red-400 hover:bg-red-500/20 transition active:scale-95 shadow-sm disabled:opacity-50"
                    >
                      {downloading === "pdf" ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                      <span>Descargar PDF</span>
                    </button>

                    <button
                      type="button"
                      disabled={downloading !== null}
                      onClick={() => safeDownload(xmlUrl, `cfdi-${currentTicket.cfdi_uuid || currentTicket.folio || currentTicket.id}.xml`, "xml")}
                      className="flex items-center gap-2 rounded-xl border border-blue-500/30 bg-blue-500/10 px-4 py-2 text-xs font-semibold text-blue-400 hover:bg-blue-500/20 transition active:scale-95 shadow-sm disabled:opacity-50"
                    >
                      {downloading === "xml" ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileCode className="h-4 w-4" />}
                      <span>Descargar XML</span>
                    </button>

                    <button
                      type="button"
                      disabled={downloading !== null}
                      onClick={() => safeDownload(zipUrl, `cfdi-${currentTicket.cfdi_uuid || currentTicket.folio || currentTicket.id}.zip`, "zip")}
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
                    {currentTicket.correo_capturado_en_portal
                      ? `El portal del comercio timbró la factura y envió los archivos XML/PDF a: ${currentTicket.correo_capturado_en_portal}`
                      : "El emisor envió la factura a tu correo registrado o el periodo de descarga efímera ha expirado."}
                  </p>
                  <p className="mt-2 text-[10px] text-muted/70">
                    FacturAI opera con Cero Retención en disco para garantizar la máxima privacidad fiscal.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Bloque si Requiere Humano o Portal */}
          {isEsperaHumano && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
              <div className="flex items-start gap-3">
                <AlertCircle className="h-5 w-5 text-amber-500 shrink-0 mt-0.5" />
                <div className="flex-1">
                  <h4 className="text-sm font-bold text-ink">
                    {isMissingPortal
                      ? "Portal de facturación requerido"
                      : "Se requiere intervención humana"}
                  </h4>
                  <p className="text-xs text-muted mt-0.5">
                    {isMissingPortal
                      ? "El comprobante impreso no contenía enlace web. Tu ticket está 100% resguardado y la imagen no se elimina. Ingresa la URL o haz clic en 'Investigar con IA' en la sección de portal abajo."
                      : "El portal del comercio solicitó resolver un captcha o verificar un dato especial."}
                  </p>
                  {!isMissingPortal && onOpenHandoff && (
                    <button
                      type="button"
                      onClick={() => onOpenHandoff(currentTicket)}
                      className="mt-3 flex items-center gap-2 rounded-xl bg-amber-500 px-4 py-2 text-xs font-bold text-white shadow hover:brightness-105 active:scale-95"
                    >
                      <Zap className="h-4 w-4" />
                      <span>Tomar control en vivo (Handoff)</span>
                    </button>
                  )}
                  {isMissingPortal && (
                    <button
                      type="button"
                      onClick={() => {
                        setIsEditingPortal(true);
                        handleDeducePortal();
                      }}
                      disabled={isSearchingPortal}
                      className="mt-3 flex items-center gap-1.5 rounded-xl bg-brand px-3.5 py-2 text-xs font-bold text-on-brand shadow hover:brightness-105 active:scale-95 disabled:opacity-50"
                    >
                      {isSearchingPortal ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Sparkles className="h-3.5 w-3.5" />
                      )}
                      <span>Investigar portal con IA ahora</span>
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
                  <h4 className="text-sm font-bold text-ink">
                    {currentTicket.error_code === "saldo_ia_agotado"
                      ? "Saldo de IA agotado en Anthropic"
                      : "Facturación no completada"}
                  </h4>
                  <p className="text-xs text-bad/90 mt-1 font-mono">
                    {currentTicket.error_msg || currentTicket.error_code || "El portal no completó la emisión."}
                  </p>

                  {currentTicket.error_code === "saldo_ia_agotado" && (
                    <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-ink">
                      <p className="font-bold text-amber-500 mb-1">¿Cómo solucionarlo?</p>
                      <p className="text-[12px] text-muted leading-relaxed">
                        Tu cuenta de Anthropic no tiene créditos disponibles para procesar la imagen con visión artificial.
                        Para continuar procesando tickets reales, agrega saldo en{" "}
                        <a
                          href="https://console.anthropic.com/settings/plans"
                          target="_blank"
                          rel="noopener noreferrer"
                          className="font-bold text-brand underline inline-flex items-center gap-1"
                        >
                          console.anthropic.com <ExternalLink className="h-3 w-3" />
                        </a>{" "}
                        o actualiza la variable <code className="rounded bg-panel px-1.5 py-0.5 font-mono text-ink">ANTHROPIC_API_KEY</code> en tu archivo <code className="rounded bg-panel px-1.5 py-0.5 font-mono text-ink">apps/api/.env</code>.
                      </p>
                    </div>
                  )}

                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => onRetry(currentTicket.id)}
                      className="flex items-center gap-2 rounded-xl border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink hover:bg-panel-2 active:scale-95 transition"
                    >
                      <RefreshCw className="h-4 w-4" />
                      <span>Reintentar facturación</span>
                    </button>
                    {!currentTicket.url_facturacion && (
                      <button
                        type="button"
                        onClick={() => setIsEditingPortal(true)}
                        className="flex items-center gap-1.5 rounded-xl bg-brand px-3.5 py-2 text-xs font-bold text-on-brand shadow hover:brightness-105 active:scale-95 transition"
                      >
                        <Globe className="h-3.5 w-3.5" />
                        <span>Asignar portal web</span>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Inteligencia Fiscal y Desglose de Impuestos SAT */}
          {(currentTicket.categoria_gasto || currentTicket.desglose_impuestos) && (
            <div className="rounded-xl border border-line bg-panel-2/40 p-4">
              <div className="flex items-center justify-between gap-2 mb-3">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold uppercase tracking-wider text-ink flex items-center gap-1.5">
                    {currentTicket.categoria_gasto === "combustible" && <Fuel className="h-4 w-4 text-amber-500" />}
                    {currentTicket.categoria_gasto === "hospedaje" && <Hotel className="h-4 w-4 text-blue-500" />}
                    {currentTicket.categoria_gasto === "restaurante" && <Utensils className="h-4 w-4 text-orange-500" />}
                    {currentTicket.categoria_gasto === "supermercado" && <ShoppingCart className="h-4 w-4 text-emerald-500" />}
                    {currentTicket.categoria_gasto === "casetas_peaje" && <Navigation className="h-4 w-4 text-indigo-500" />}
                    {currentTicket.categoria_gasto === "vuelos_transporte" && <Plane className="h-4 w-4 text-cyan-500" />}
                    <span>
                      {currentTicket.categoria_gasto === "combustible" ? "Combustible / Gasolina" :
                       currentTicket.categoria_gasto === "hospedaje" ? "Hospedaje / Hotel" :
                       currentTicket.categoria_gasto === "restaurante" ? "Alimentos / Restaurante" :
                       currentTicket.categoria_gasto === "supermercado" ? "Supermercado / Despensa" :
                       currentTicket.categoria_gasto === "casetas_peaje" ? "Casetas / Peaje" :
                       currentTicket.categoria_gasto === "vuelos_transporte" ? "Transporte / Vuelos" :
                       currentTicket.categoria_gasto === "servicios_generales" ? "Servicios Generales" : "Gasto General"}
                    </span>
                  </span>
                </div>

                {/* Semáforo Deducibilidad SAT */}
                {currentTicket.estatus_deducibilidad && (
                  <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-bold ${
                    currentTicket.estatus_deducibilidad === "deducible_100" ? "bg-ok-soft text-ok border border-ok/30" :
                    currentTicket.estatus_deducibilidad === "no_deducible" ? "bg-bad/15 text-bad border border-bad/30" :
                    "bg-amber-500/15 text-amber-400 border border-amber-500/30"
                  }`}>
                    {currentTicket.estatus_deducibilidad === "deducible_100" ? <ShieldCheck className="h-3.5 w-3.5" /> : <ShieldAlert className="h-3.5 w-3.5" />}
                    <span>
                      {currentTicket.estatus_deducibilidad === "deducible_100" ? "100% Deducible SAT" :
                       currentTicket.estatus_deducibilidad === "no_deducible" ? "No Deducible (Efectivo)" :
                       currentTicket.estatus_deducibilidad === "deducible_parcial" ? "8.5% Deducible (Local)" : "Deducible Condicionado"}
                    </span>
                  </span>
                )}
              </div>

              {/* Desglose de Impuestos */}
              {currentTicket.desglose_impuestos && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-2 border-t border-line/60 text-xs">
                  <div className="rounded-lg bg-panel p-2.5 border border-line/50">
                    <span className="text-[10px] text-muted block font-medium">IVA 16%</span>
                    <span className="mono font-bold text-ink">
                      {formatCurrency(currentTicket.desglose_impuestos.iva_16 ?? 0)}
                    </span>
                    <span className="text-[10px] text-muted block mt-0.5">
                      Base: {formatCurrency(currentTicket.desglose_impuestos.base_16 ?? 0)}
                    </span>
                  </div>

                  <div className="rounded-lg bg-panel p-2.5 border border-line/50">
                    <span className="text-[10px] text-muted block font-medium">Tasa 0%</span>
                    <span className="mono font-bold text-ink">
                      {formatCurrency(currentTicket.desglose_impuestos.base_0 ?? 0)}
                    </span>
                    <span className="text-[10px] text-muted block mt-0.5">IVA: $0.00</span>
                  </div>

                  <div className="rounded-lg bg-panel p-2.5 border border-line/50">
                    <span className="text-[10px] text-muted block font-medium">
                      {(currentTicket.desglose_impuestos.ish && currentTicket.desglose_impuestos.ish > 0) ? "ISH (Hospedaje)" :
                       (currentTicket.desglose_impuestos.ieps && currentTicket.desglose_impuestos.ieps > 0) ? "IEPS" : "Exento"}
                    </span>
                    <span className="mono font-bold text-ink">
                      {formatCurrency(
                        (currentTicket.desglose_impuestos.ish && currentTicket.desglose_impuestos.ish > 0)
                          ? currentTicket.desglose_impuestos.ish
                          : (currentTicket.desglose_impuestos.ieps && currentTicket.desglose_impuestos.ieps > 0)
                          ? currentTicket.desglose_impuestos.ieps
                          : (currentTicket.desglose_impuestos.base_exenta ?? 0)
                      )}
                    </span>
                    <span className="text-[10px] text-muted block mt-0.5">
                      {(currentTicket.desglose_impuestos.ish && currentTicket.desglose_impuestos.ish > 0) ? "Impuesto local" :
                       (currentTicket.desglose_impuestos.ieps && currentTicket.desglose_impuestos.ieps > 0) ? "Cuota / Tasa" : "Sin IVA"}
                    </span>
                  </div>

                  <div className="rounded-lg bg-panel p-2.5 border border-line/50">
                    <span className="text-[10px] text-muted block font-medium">Retenciones</span>
                    <span className="mono font-bold text-ink">
                      {formatCurrency(
                        ((currentTicket.desglose_impuestos.retencion_iva ?? 0) + (currentTicket.desglose_impuestos.retencion_isr ?? 0))
                      )}
                    </span>
                    <span className="text-[10px] text-muted block mt-0.5">IVA / ISR</span>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Auditoría Matemática SAT Anexo 20 y Blindaje Fiscal Art. 69-B */}
          {(currentTicket.auditoria_aritmetica || currentTicket.score_riesgo_fiscal !== undefined) && (
            <div className="rounded-2xl border border-line-2 bg-panel-2/40 p-4">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <ShieldCheck className="h-4 w-4 text-brand" />
                  <h4 className="text-xs font-bold uppercase tracking-wider text-ink">
                    Auditoría Matemática SAT & Blindaje Fiscal
                  </h4>
                </div>
                {currentTicket.auditoria_aritmetica?.es_valido_anexo_20 && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-ok/10 px-2.5 py-0.5 text-[10px] font-bold text-ok">
                    <Check className="h-3 w-3" />
                    Sello Anexo 20 SAT Válido
                  </span>
                )}
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {/* Score Matemático Anexo 20 */}
                <div className="rounded-xl border border-line bg-panel p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-muted font-medium">Consistencia Aritmética</span>
                    <span className="mono text-xs font-bold text-ink">
                      {currentTicket.auditoria_aritmetica?.score_matematico ?? 100} / 100
                    </span>
                  </div>
                  <div className="mt-2 text-xs space-y-1">
                    <div className="flex justify-between text-muted">
                      <span>Total declarado:</span>
                      <span className="mono font-semibold text-ink">
                        {formatCurrency(currentTicket.auditoria_aritmetica?.total_declarado ?? currentTicket.total ?? 0)}
                      </span>
                    </div>
                    <div className="flex justify-between text-muted">
                      <span>Total calculado:</span>
                      <span className="mono font-semibold text-ink">
                        {formatCurrency(currentTicket.auditoria_aritmetica?.total_calculado ?? currentTicket.total ?? 0)}
                      </span>
                    </div>
                    {currentTicket.auditoria_aritmetica?.tasa_efectiva_iva !== undefined && currentTicket.auditoria_aritmetica?.tasa_efectiva_iva !== null && (
                      <div className="flex justify-between text-muted">
                        <span>Tasa efectiva IVA:</span>
                        <span className="mono font-semibold text-ink">
                          {(currentTicket.auditoria_aritmetica.tasa_efectiva_iva * 100).toFixed(2)}%
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                {/* Score de Riesgo Fiscal Art. 69-B */}
                <div className="rounded-xl border border-line bg-panel p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-muted font-medium">Score de Riesgo Fiscal SAT</span>
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold ${
                      (currentTicket.score_riesgo_fiscal ?? 0) <= 25
                        ? "bg-ok/10 text-ok"
                        : (currentTicket.score_riesgo_fiscal ?? 0) <= 65
                        ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
                        : "bg-bad/10 text-bad"
                    }`}>
                      {(currentTicket.score_riesgo_fiscal ?? 0) <= 25 ? "Riesgo Bajo" : (currentTicket.score_riesgo_fiscal ?? 0) <= 65 ? "Riesgo Medio" : "Riesgo Alto"} ({currentTicket.score_riesgo_fiscal ?? 0}/100)
                    </span>
                  </div>
                  <p className="mt-2 text-xs text-muted leading-relaxed">
                    {(currentTicket.score_riesgo_fiscal ?? 0) <= 25
                      ? "Sin antecedentes en listas negras Art. 69-B CFF ni inconsistencias de deducibilidad."
                      : (currentTicket.score_riesgo_fiscal ?? 0) <= 65
                      ? "Requiere comprobante de pago electrónico o documentación de respaldo para el SAT."
                      : "Alerta crítica de fiscalización. Riesgo de no deducibilidad o auditoría SAT."}
                  </p>
                </div>
              </div>

              {/* Hash Criptográfico de Integridad SHA-256 */}
              {currentTicket.hash_integridad && (
                <div className="mt-3 flex items-center justify-between rounded-xl border border-line bg-panel p-2.5 text-xs">
                  <div className="flex items-center gap-2 overflow-hidden">
                    <Fingerprint className="h-3.5 w-3.5 shrink-0 text-muted" />
                    <span className="text-[11px] text-muted shrink-0">Hash SHA-256:</span>
                    <span className="mono text-[10px] text-ink truncate" title={currentTicket.hash_integridad}>
                      {currentTicket.hash_integridad}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      navigator.clipboard.writeText(currentTicket.hash_integridad || "");
                      setCopiedHash(true);
                      setTimeout(() => setCopiedHash(false), 2000);
                    }}
                    className="ml-2 flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-medium text-muted hover:bg-panel-2 hover:text-ink transition shrink-0"
                  >
                    {copiedHash ? <Check className="h-3 w-3 text-ok" /> : <Copy className="h-3 w-3" />}
                    <span>{copiedHash ? "Copiado" : "Copiar"}</span>
                  </button>
                </div>
              )}
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
                  {formatCurrency(currentTicket.total)}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Subtotal</span>
                <p className="mono text-sm font-semibold text-ink mt-1">
                  {currentTicket.subtotal ? formatCurrency(currentTicket.subtotal) : "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">IVA</span>
                <p className="mono text-sm font-semibold text-ink mt-1">
                  {currentTicket.iva ? formatCurrency(currentTicket.iva) : "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Folio / Ticket</span>
                <p className="mono text-sm font-semibold text-ink mt-1">
                  {currentTicket.folio || "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Establecimiento / Emisor</span>
                <p className="text-xs font-semibold text-ink mt-1 truncate" title={currentTicket.sucursal || ""}>
                  {currentTicket.sucursal || "—"}
                </p>
              </div>

              <div className="rounded-xl border border-line bg-panel p-3">
                <span className="text-[11px] text-muted">Caja / Transacción</span>
                <p className="mono text-xs font-semibold text-ink mt-1">
                  {currentTicket.caja || currentTicket.web_id || "—"}
                </p>
              </div>
            </div>
          </div>

          {/* Portal de Facturación Investigado */}
          <div className="rounded-2xl border border-line-2 bg-panel-2/40 p-4">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <Globe className="h-4 w-4 text-brand" />
                <h4 className="text-xs font-bold uppercase tracking-wider text-ink">
                  Portal de Facturación del Comercio
                </h4>
              </div>
              {!isEditingPortal && (
                <button
                  type="button"
                  onClick={() => setIsEditingPortal(true)}
                  className="flex items-center gap-1 text-[11px] font-semibold text-brand hover:underline transition"
                >
                  <Edit3 className="h-3 w-3" />
                  <span>{currentTicket.url_facturacion ? "Cambiar portal" : "Asignar portal"}</span>
                </button>
              )}
            </div>

            {!isEditingPortal ? (
              <div className="flex items-center justify-between rounded-xl border border-line bg-panel p-3">
                <div className="min-w-0 flex-1 pr-2">
                  <p className="text-[11px] text-muted">URL configurada para emisión de CFDI:</p>
                  {currentTicket.url_facturacion ? (
                    <a
                      href={currentTicket.url_facturacion}
                      target="_blank"
                      rel="noreferrer"
                      className="flex items-center gap-1.5 text-xs font-semibold text-brand hover:underline mt-0.5 truncate"
                    >
                      <span className="truncate">{currentTicket.url_facturacion}</span>
                      <ExternalLink className="h-3 w-3 shrink-0" />
                    </a>
                  ) : (
                    <p className="text-xs text-amber-500 font-medium italic mt-0.5">
                      Sin URL asignada. Haz clic en &ldquo;Asignar portal&rdquo; o &ldquo;Investigar con IA&rdquo;.
                    </p>
                  )}
                </div>
              </div>
            ) : (
              <div className="space-y-3 rounded-xl border border-line bg-panel p-3.5">
                <div>
                  <label className="block text-[11px] font-semibold text-muted mb-1">
                    Enlace / Portal web de facturación:
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="url"
                      value={portalInput}
                      onChange={(e) => setPortalInput(e.target.value)}
                      placeholder="https://facturacion.comercio.com"
                      className="flex-1 rounded-xl border border-line-2 bg-panel-2 px-3 py-2 text-xs text-ink placeholder:text-muted/60 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand mono"
                      disabled={isSavingPortal || isSearchingPortal}
                    />
                    <button
                      type="button"
                      disabled={isSearchingPortal || isSavingPortal}
                      onClick={handleDeducePortal}
                      title="Investigar portal automáticamente con IA a partir de marca, sucursal y RFC"
                      className="flex items-center gap-1.5 rounded-xl border border-brand/40 bg-brand/10 px-3 py-2 text-xs font-bold text-brand hover:bg-brand/20 transition active:scale-95 disabled:opacity-50 shrink-0"
                    >
                      {isSearchingPortal ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Sparkles className="h-3.5 w-3.5 text-brand" />
                      )}
                      <span>Investigar con IA</span>
                    </button>
                  </div>
                </div>

                {/* Candidatos sugeridos por IA */}
                {portalCandidates.length > 0 && (
                  <div className="rounded-lg bg-panel-2/60 p-2.5 border border-line/60">
                    <span className="text-[10px] font-bold uppercase tracking-wider text-muted block mb-1.5">
                      Portales sugeridos por IA (haz clic para seleccionar):
                    </span>
                    <div className="flex flex-wrap gap-1.5">
                      {portalCandidates.map((cand, idx) => (
                        <button
                          key={idx}
                          type="button"
                          onClick={() => setPortalInput(cand)}
                          className={`flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-mono transition border ${
                            portalInput === cand
                              ? "bg-brand/20 text-brand border-brand/50 font-bold"
                              : "bg-panel text-muted hover:text-ink hover:border-line-2 border-line"
                          }`}
                        >
                          <Globe className="h-3 w-3" />
                          <span className="max-w-[260px] truncate">{cand}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {/* Mensajes de feedback */}
                {portalFeedback && (
                  <div
                    className={`rounded-lg px-3 py-2 text-xs font-medium ${
                      portalFeedback.type === "success"
                        ? "bg-ok-soft text-ok border border-ok/30"
                        : portalFeedback.type === "error"
                        ? "bg-bad/10 text-bad border border-bad/30"
                        : "bg-panel-2 text-muted border border-line"
                    }`}
                  >
                    {portalFeedback.text}
                  </div>
                )}

                {/* Botones de acción del formulario */}
                <div className="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-line/50">
                  {currentTicket.url_facturacion ? (
                    <button
                      type="button"
                      disabled={isSavingPortal}
                      onClick={() => {
                        setIsEditingPortal(false);
                        setPortalInput(currentTicket.url_facturacion || "");
                        setPortalFeedback(null);
                      }}
                      className="text-xs text-muted hover:text-ink transition"
                    >
                      Cancelar
                    </button>
                  ) : (
                    <span className="text-[11px] text-muted">
                      FacturAI intentará emitir tu factura en esta dirección.
                    </span>
                  )}

                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={isSavingPortal || !portalInput.trim()}
                      onClick={() => handleSavePortal(false)}
                      className="flex items-center gap-1.5 rounded-xl border border-line-2 bg-panel px-3 py-1.5 text-xs font-semibold text-ink hover:bg-panel-2 transition disabled:opacity-50"
                    >
                      {isSavingPortal ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Save className="h-3.5 w-3.5" />
                      )}
                      <span>Solo guardar</span>
                    </button>

                    <button
                      type="button"
                      disabled={isSavingPortal || !portalInput.trim()}
                      onClick={() => handleSavePortal(true)}
                      className="flex items-center gap-1.5 rounded-xl bg-brand px-3.5 py-1.5 text-xs font-bold text-on-brand shadow hover:brightness-105 transition active:scale-95 disabled:opacity-50"
                    >
                      {isSavingPortal ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Play className="h-3.5 w-3.5 fill-current" />
                      )}
                      <span>Guardar y Facturar</span>
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Métricas y Auditoría */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted mb-2">
              Bóveda Fiscal Cifrada y Auditoría
            </h4>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 text-xs">
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Intentos</span>
                <p className="mono font-semibold text-ink">{currentTicket.intentos} / 3</p>
              </div>
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Pasos Agente</span>
                <p className="mono font-semibold text-ink">{currentTicket.pasos_agente || 1}</p>
              </div>
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Duración</span>
                <p className="mono font-semibold text-ink">
                  {currentTicket.duracion_segundos ? `${Math.round(currentTicket.duracion_segundos)}s` : "—"}
                </p>
              </div>
              <div className="rounded-xl border border-line bg-panel p-2.5">
                <span className="text-[10px] text-muted">Imagen Temporal</span>
                <p className="font-semibold text-ok">
                  {!currentTicket.image_key || isFacturado ? "✓ Eliminada" : "En proceso"}
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Footer Actions */}
        <div className="flex items-center justify-between border-t border-line bg-panel-2/50 px-6 py-3">
          <span className="text-[11px] text-muted">
            ID: <span className="mono">{currentTicket.id.slice(0, 8)}</span>
          </span>
          <div className="flex items-center gap-2">
            {isExtraido && (
              <button
                type="button"
                onClick={() => {
                  onFacturar(currentTicket.id);
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
