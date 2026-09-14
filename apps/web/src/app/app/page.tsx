"use client";

import React, { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { deleteTicket, facturarTicket, getTickets, retryTicket } from "@/lib/api";
import { TicketResponse } from "@/types/api";
import { StatusBadge } from "@/components/StatusBadge";
import { CfdiDownloadButton } from "@/components/CfdiDownloadButton";
import { MobileCameraUpload } from "@/components/MobileCameraUpload";
import { HandoffModal } from "@/components/HandoffModal";
import { TicketDetailModal } from "@/components/TicketDetailModal";
import { useRouter } from "next/navigation";
import { formatCurrency, formatDate } from "@/lib/utils";
import {
  Plus,
  Play,
  RefreshCw,
  Trash2,
  AlertCircle,
  Clock,
  CheckCircle2,
  Database,
  X,
  Receipt,
  Eye,
} from "lucide-react";

export default function TicketsPage() {
  const router = useRouter();
  const { activeTenant } = useAuth();
  const [tickets, setTickets] = useState<TicketResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterState, setFilterState] = useState<string>("todos");
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [selectedTicketForHandoff, setSelectedTicketForHandoff] =
    useState<TicketResponse | null>(null);
  const [selectedTicketDetail, setSelectedTicketDetail] =
    useState<TicketResponse | null>(null);

  const tenantId = activeTenant?.id;

  const loadTickets = async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const resp = await getTickets(tenantId);
      setTickets(resp.items || []);
    } catch (err) {
      console.error("Error al cargar tickets:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTickets();
  }, [tenantId]);

  // Filtrado en memoria
  const filteredTickets = tickets.filter((t) => {
    if (filterState === "todos") return true;
    if (filterState === "humano") return t.estado === "espera_humano";
    if (filterState === "facturados") return t.estado === "facturado";
    if (filterState === "error")
      return t.estado === "rechazado" || t.estado === "cancelado";
    return true;
  });

  // Métricas para los KPIs
  const totalMes = tickets.length;
  const facturadosCount = tickets.filter((t) => t.estado === "facturado").length;
  const porcentajeAutomatico =
    totalMes > 0 ? Math.round((facturadosCount / totalMes) * 100) : 100;
  const pendientesHumano = tickets.filter(
    (t) => t.estado === "espera_humano"
  ).length;

  const handleFacturar = async (tId: string) => {
    if (!tenantId) return;
    try {
      await facturarTicket(tenantId, tId);
      await loadTickets();
    } catch (err: unknown) {
      const msg = (err as Error).message || "";
      if (
        msg.toLowerCase().includes("perfil") ||
        msg.toLowerCase().includes("fiscal") ||
        msg.toLowerCase().includes("rfc")
      ) {
        if (
          confirm(
            "No tienes configurado tu perfil fiscal principal. ¿Deseas ir a 'Datos fiscales' para configurarlo ahora?"
          )
        ) {
          router.push("/app/datos");
          return;
        }
      }
      alert(msg || "No se pudo iniciar la facturación del ticket.");
    }
  };

  const handleRetry = async (tId: string) => {
    if (!tenantId) return;
    try {
      await retryTicket(tenantId, tId);
      await loadTickets();
    } catch (err: unknown) {
      alert((err as Error).message || "No se pudo reintentar el ticket.");
    }
  };

  const handleDelete = async (tId: string) => {
    if (!tenantId) return;
    if (!confirm("¿Eliminar este ticket y su historial? Esta acción es inmediata."))
      return;
    try {
      await deleteTicket(tenantId, tId);
      await loadTickets();
    } catch (err: unknown) {
      alert((err as Error).message || "Error al eliminar el ticket.");
    }
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Encabezado */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-ink">Tickets</h1>
          <p className="mt-0.5 text-xs text-muted">
            {pendientesHumano > 0
              ? `${pendientesHumano} esperando algo tuyo`
              : "Todo en orden · Cola sincronizada"}
          </p>
        </div>

        <button
          type="button"
          onClick={() => setShowUploadModal(true)}
          className="flex items-center gap-2 rounded-xl bg-brand px-4 py-2.5 text-xs font-bold text-on-brand shadow-sm transition-all hover:brightness-105 active:scale-95"
        >
          <Plus className="h-4 w-4 stroke-[2.5]" />
          <span>Subir ticket</span>
        </button>
      </div>

      {/* Grid de 4 Indicadores (KPIs) - docs/prototipo.html:299-304 */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-xl border border-line bg-panel p-3.5 shadow-sm">
          <p className="lt">Este mes</p>
          <div className="mono mt-1 text-2xl font-bold text-ink">{totalMes}</div>
          <p className="mt-0.5 text-[11px] text-muted">tickets procesados</p>
        </div>

        <div className="rounded-xl border border-line bg-panel p-3.5 shadow-sm">
          <p className="lt">Automático</p>
          <div className="mono mt-1 text-2xl font-bold text-brand">
            {porcentajeAutomatico}%
          </div>
          <p className="mt-0.5 text-[11px] text-muted">sin tocar nada</p>
        </div>

        <div className="rounded-xl border border-line bg-panel p-3.5 shadow-sm">
          <p className="lt">Tiempo medio</p>
          <div className="mono mt-1 text-2xl font-bold text-ink">
            42<span className="text-xs font-normal">s</span>
          </div>
          <p className="mt-0.5 text-[11px] text-muted">de foto a timbrado</p>
        </div>

        <div className="rounded-xl border border-line bg-panel p-3.5 shadow-sm">
          <p className="lt">Almacenado</p>
          <div className="mono mt-1 text-2xl font-bold text-ink">
            0<span className="text-xs font-normal">MB</span>
          </div>
          <p className="mt-0.5 text-[11px] text-muted">imágenes y CFDI</p>
        </div>
      </div>

      {/* Tarjeta de Cola de Facturación con Filtros */}
      <div className="overflow-hidden rounded-2xl border border-line bg-panel shadow-prototipo">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
          <h2 className="text-sm font-bold text-ink">Cola de facturación</h2>

          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => setFilterState("todos")}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                filterState === "todos"
                  ? "bg-ink text-bg font-semibold"
                  : "border border-line-2 bg-panel-2 text-ink-2 hover:bg-line/20"
              }`}
            >
              Todos
            </button>
            <button
              type="button"
              onClick={() => setFilterState("humano")}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                filterState === "humano"
                  ? "bg-ink text-bg font-semibold"
                  : "border border-line-2 bg-panel-2 text-ink-2 hover:bg-line/20"
              }`}
            >
              Requieren humano
            </button>
            <button
              type="button"
              onClick={() => setFilterState("facturados")}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                filterState === "facturados"
                  ? "bg-ink text-bg font-semibold"
                  : "border border-line-2 bg-panel-2 text-ink-2 hover:bg-line/20"
              }`}
            >
              Facturados
            </button>
            <button
              type="button"
              onClick={() => setFilterState("error")}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                filterState === "error"
                  ? "bg-ink text-bg font-semibold"
                  : "border border-line-2 bg-panel-2 text-ink-2 hover:bg-line/20"
              }`}
            >
              Con error
            </button>
          </div>
        </div>

        {/* Tabla de Tickets */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-line bg-panel-2/50 text-[10px] font-semibold uppercase tracking-wider text-muted">
                <th className="px-4 py-2.5">Comercio</th>
                <th className="px-4 py-2.5">Folio</th>
                <th className="px-4 py-2.5">Monto</th>
                <th className="px-4 py-2.5">Estado</th>
                <th className="px-4 py-2.5">Imagen</th>
                <th className="px-4 py-2.5">Fecha</th>
                <th className="px-4 py-2.5 text-right">Acción</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {filteredTickets.map((t) => {
                const imageEliminada = !t.image_key || t.estado === "facturado";
                return (
                  <tr
                    key={t.id}
                    className="transition-colors hover:bg-panel-2 cursor-pointer group"
                    onClick={() => setSelectedTicketDetail(t)}
                  >
                    <td className="px-4 py-3">
                      <b className="block text-sm font-semibold text-ink group-hover:text-brand transition-colors">
                        {t.sucursal || t.rfc_emisor || "Comercio"}
                      </b>
                      <span className="text-[11px] text-muted">
                        {t.rfc_emisor || "RFC en proceso"}
                      </span>
                    </td>

                    <td className="mono px-4 py-3 text-xs text-ink">
                      {t.folio || "—"}
                    </td>

                    <td className="mono px-4 py-3 text-xs font-bold text-ink">
                      {formatCurrency(t.total)}
                    </td>

                    <td className="px-4 py-3">
                      <StatusBadge
                        estado={t.estado}
                        errorCode={t.error_code}
                        errorMsg={t.error_msg}
                      />
                      {t.estado === "rechazado" && t.error_msg && (
                        <p
                          className="mt-1 max-w-[180px] truncate text-[10px] text-bad/90"
                          title={t.error_msg}
                        >
                          {t.error_msg}
                        </p>
                      )}
                    </td>

                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center gap-1 text-[11px] font-medium ${
                          imageEliminada ? "text-ok" : "text-muted"
                        }`}
                      >
                        {imageEliminada ? "✓ eliminada" : "en proceso"}
                      </span>
                    </td>

                    <td className="px-4 py-3 text-[11px] text-muted">
                      {formatDate(t.fecha_ticket || t.created_at)}
                    </td>

                    <td
                      className="px-4 py-3 text-right"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <div className="flex items-center justify-end gap-1.5">
                        {/* Botón Facturar si está extraído */}
                        {t.estado === "extraido" && (
                          <button
                            type="button"
                            onClick={() => handleFacturar(t.id)}
                            title="Iniciar facturación automática"
                            className="flex items-center gap-1.5 rounded-lg bg-brand px-2.5 py-1 text-[11px] font-bold text-on-brand shadow-sm transition-all hover:brightness-105 active:scale-95"
                          >
                            <Play className="h-3 w-3 fill-current" />
                            <span>Facturar</span>
                          </button>
                        )}

                        {/* Botón Handoff si requiere intervención */}
                        {t.estado === "espera_humano" && (
                          <button
                            type="button"
                            onClick={() => setSelectedTicketForHandoff(t)}
                            title="Tomar control en vivo para resolver captcha"
                            className="flex items-center gap-1 rounded-lg bg-amber-500 px-2 py-1 text-[11px] font-bold text-white shadow-sm hover:brightness-105 active:scale-95"
                          >
                            <span>Resolver</span>
                          </button>
                        )}

                        {/* Botones de descarga si ya está facturado */}
                        {t.estado === "facturado" && (
                          <CfdiDownloadButton
                            ticketId={t.id}
                            disponibleHasta={t.cfdi_disponible_hasta}
                            tenantId={tenantId}
                            compact={true}
                          />
                        )}

                        {/* Botón de ver detalle */}
                        <button
                          type="button"
                          onClick={() => setSelectedTicketDetail(t)}
                          title="Ver detalle del ticket"
                          className="flex h-7 w-7 items-center justify-center rounded-lg border border-line-2 bg-panel text-muted hover:text-ink transition"
                        >
                          <Eye className="h-3.5 w-3.5" />
                        </button>

                        {/* Botón de reintento si falló */}
                        {(t.estado === "rechazado" || t.estado === "cancelado") && (
                          <button
                            type="button"
                            onClick={() => handleRetry(t.id)}
                            title="Reintentar facturación"
                            className="flex h-7 w-7 items-center justify-center rounded-lg border border-line-2 bg-panel text-muted hover:text-brand"
                          >
                            <RefreshCw className="h-3.5 w-3.5" />
                          </button>
                        )}

                        {/* Botón de eliminar */}
                        <button
                          type="button"
                          onClick={() => handleDelete(t.id)}
                          title="Eliminar ticket"
                          className="flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:text-bad"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}

              {/* Estado Vacío */}
              {!loading && filteredTickets.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-12 text-center">
                    <div className="mx-auto flex max-w-xs flex-col items-center gap-2 text-muted">
                      <Receipt className="h-8 w-8 stroke-[1.5] text-line-2" />
                      <b className="text-sm font-semibold text-ink">
                        No hay tickets registrados
                      </b>
                      <p className="text-xs">
                        Presiona &ldquo;Subir ticket&rdquo; para fotografiar tu
                        primer comprobante en la tienda.
                      </p>
                      <button
                        type="button"
                        onClick={() => setShowUploadModal(true)}
                        className="mt-2 rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand shadow hover:brightness-105"
                      >
                        ＋ Subir mi primer ticket
                      </button>
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* MODAL: SUBIR TICKET CON CÁMARA */}
      {showUploadModal && tenantId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-md rounded-2xl border border-line bg-panel p-5 shadow-2xl">
            <div className="mb-4 flex items-start justify-between">
              <div>
                <h3 className="text-base font-bold text-ink">Subir ticket</h3>
                <p className="text-xs text-muted">
                  Una foto legible basta. El agente hace el resto.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowUploadModal(false)}
                className="flex h-7 w-7 items-center justify-center rounded-lg bg-panel-2 text-muted hover:text-ink"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <MobileCameraUpload
              tenantId={tenantId}
              onSuccess={() => {
                loadTickets();
              }}
              onClose={() => setShowUploadModal(false)}
            />
          </div>
        </div>
      )}

      {/* MODAL: INTERVENCIÓN HUMANA EN VIVO (Handoff interactivo) */}
      {selectedTicketForHandoff && tenantId && (
        <HandoffModal
          tenantId={tenantId}
          ticketId={selectedTicketForHandoff.id}
          onClose={() => setSelectedTicketForHandoff(null)}
          onSuccess={() => {
            setSelectedTicketForHandoff(null);
            loadTickets();
          }}
        />
      )}

      {/* MODAL: DETALLE COMPLETO Y DESCARGA DIRECTA DE CFDI */}
      {selectedTicketDetail && (
        <TicketDetailModal
          ticket={selectedTicketDetail}
          tenantId={tenantId}
          onClose={() => setSelectedTicketDetail(null)}
          onFacturar={handleFacturar}
          onRetry={handleRetry}
          onOpenHandoff={(t) => {
            setSelectedTicketDetail(null);
            setSelectedTicketForHandoff(t);
          }}
        />
      )}
    </div>
  );
}
