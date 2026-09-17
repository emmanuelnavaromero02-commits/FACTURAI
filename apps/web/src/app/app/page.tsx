"use client";

import React, { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { deleteTicket, facturarTicket, getTickets, retryTicket } from "@/lib/api";
import { TicketResponse } from "@/types/api";
import { StatusBadge } from "@/components/StatusBadge";
import { CfdiDownloadButton } from "@/components/CfdiDownloadButton";
import { MobileCameraUpload } from "@/components/MobileCameraUpload";
import { DirectCfdiUpload } from "@/components/DirectCfdiUpload";
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
  Camera,
  FileCode,
  FileText,
  Fuel,
  Hotel,
  Utensils,
  ShoppingCart,
  Navigation,
  Plane,
  Briefcase,
  Layers,
  ShieldCheck,
  ShieldAlert,
  Globe,
  Zap,
} from "lucide-react";

const CATEGORIAS = [
  { id: "todos", label: "Todas las categorías", icon: Layers, satRule: "Vista general de comprobantes" },
  { id: "restaurante", label: "Restaurantes y Alimentos", icon: Utensils, satRule: "Art. 28 Fracc. XX LISR: 8.5% deducible local / 100% en viáticos" },
  { id: "combustible", label: "Gasolina y Combustibles", icon: Fuel, satRule: "Art. 27 Fracc. III LISR: Efectivo es 0% deducible. Requiere tarjeta/banco" },
  { id: "hospedaje", label: "Hoteles y Hospedaje", icon: Hotel, satRule: "100% deducible en viáticos de trabajo. Incluye ISH local" },
  { id: "supermercado", label: "Supermercados y Despensa", icon: ShoppingCart, satRule: "Separación automática de Tasa 0% (alimentos) e IVA 16%" },
  { id: "casetas_peaje", label: "Casetas y Peajes", icon: Navigation, satRule: "100% deducible para viáticos carreteros (CAPUFE / TAG)" },
  { id: "vuelos_transporte", label: "Vuelos y Transporte", icon: Plane, satRule: "100% deducible en transporte de trabajo. Desglose de TUA e IVA" },
  { id: "servicios_generales", label: "Servicios Generales", icon: Briefcase, satRule: "100% deducible gastos de operación con CFDI" },
];

function getCategoryInfo(cat?: string | null) {
  switch (cat) {
    case "combustible":
      return { label: "Gasolina", icon: Fuel, color: "text-amber-500 bg-amber-500/10 border-amber-500/30" };
    case "hospedaje":
      return { label: "Hospedaje", icon: Hotel, color: "text-blue-400 bg-blue-500/10 border-blue-500/30" };
    case "restaurante":
      return { label: "Restaurante", icon: Utensils, color: "text-orange-400 bg-orange-500/10 border-orange-500/30" };
    case "supermercado":
      return { label: "Supermercado", icon: ShoppingCart, color: "text-emerald-400 bg-emerald-500/10 border-emerald-500/30" };
    case "casetas_peaje":
      return { label: "Caseta", icon: Navigation, color: "text-indigo-400 bg-indigo-500/10 border-indigo-500/30" };
    case "vuelos_transporte":
      return { label: "Vuelo", icon: Plane, color: "text-cyan-400 bg-cyan-500/10 border-cyan-500/30" };
    case "servicios_generales":
      return { label: "Servicios", icon: Briefcase, color: "text-purple-400 bg-purple-500/10 border-purple-500/30" };
    default:
      return { label: "General", icon: Layers, color: "text-muted bg-panel-2 border-line" };
  }
}

function getDeducibilidadBadge(estatus?: string | null) {
  if (estatus === "deducible_100") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-ok/30 bg-ok-soft px-2 py-0.5 text-[10px] font-bold text-ok">
        <ShieldCheck className="h-3 w-3" />
        <span>100% Deducible</span>
      </span>
    );
  }
  if (estatus === "deducible_parcial") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-[10px] font-bold text-amber-400">
        <ShieldAlert className="h-3 w-3" />
        <span>8.5% Art. 28</span>
      </span>
    );
  }
  if (estatus === "no_deducible") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-bad/30 bg-bad/15 px-2 py-0.5 text-[10px] font-bold text-bad">
        <ShieldAlert className="h-3 w-3" />
        <span>0% No Deducible</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-line bg-panel-2 px-2 py-0.5 text-[10px] font-medium text-muted">
      <span>Auto SAT</span>
    </span>
  );
}

export default function TicketsPage() {
  const router = useRouter();
  const { activeTenant } = useAuth();
  const [tickets, setTickets] = useState<TicketResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterState, setFilterState] = useState<string>("todos");
  const [categoriaFilter, setCategoriaFilter] = useState<string>("todos");
  const [viewMode, setViewMode] = useState<"categorias" | "tabla">("categorias");
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [uploadModalTab, setUploadModalTab] = useState<"foto" | "cfdi">("foto");
  const [selectedTicketForHandoff, setSelectedTicketForHandoff] =
    useState<TicketResponse | null>(null);
  const [selectedTicketDetail, setSelectedTicketDetail] =
    useState<TicketResponse | null>(null);

  const tenantId = activeTenant?.id;

  const loadTickets = async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const resp = await getTickets(
        tenantId,
        filterState === "todos" ? undefined : filterState,
        categoriaFilter === "todos" ? undefined : categoriaFilter
      );
      setTickets(resp.items || []);
    } catch (err) {
      console.error("Error al cargar tickets:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTickets();
  }, [tenantId, filterState, categoriaFilter]);

  // Sondeo en segundo plano (4s si hay tickets en proceso/espera, 15s si todo está estático)
  useEffect(() => {
    if (!tenantId) return;

    const tieneActivos = tickets.some((t) =>
      ["recibido", "extrayendo", "encolado", "facturando", "espera_humano"].includes(
        t.estado
      )
    );
    const intervalMs = tieneActivos ? 4000 : 15000;

    const timer = setInterval(() => {
      getTickets(tenantId)
        .then((resp) => setTickets(resp.items || []))
        .catch((err) => console.debug("Error en sondeo de tickets:", err));
    }, intervalMs);

    return () => clearInterval(timer);
  }, [tenantId, tickets]);

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

  // Agrupación automática por categorías para que NO estén todos juntos
  const ticketsPorCategoria = CATEGORIAS.filter((c) => c.id !== "todos").map((cat) => {
    const catTickets = filteredTickets.filter(
      (t) => (t.categoria_gasto || "otros") === cat.id
    );
    const totalMonto = catTickets.reduce(
      (acc, t) => acc + (parseFloat(t.total || "0") || 0),
      0
    );
    const catInfo = getCategoryInfo(cat.id);
    return {
      ...cat,
      color: catInfo.color,
      tickets: catTickets,
      totalMonto,
    };
  });

  const categoriasConGastos = ticketsPorCategoria.filter((c) => c.tickets.length > 0);
  const categoriasVacias = ticketsPorCategoria.filter((c) => c.tickets.length === 0);

  const renderTicketRow = (t: TicketResponse, hideCategoryBadge: boolean = false) => {
    const imageEliminada = !t.image_key || t.estado === "facturado";
    const catInfo = getCategoryInfo(t.categoria_gasto);
    const CatIcon = catInfo.icon;
    const isLiveCaptcha =
      t.estado === "espera_humano" &&
      (t.error_code === "captcha_requerido" || t.error_code === "intervencion_humana");
    const isMissingPortal =
      t.estado === "espera_humano" &&
      (!t.url_facturacion ||
        t.error_code === "esperando_url_portal" ||
        t.error_code === "portal_requerido");
    const canRetry =
      t.estado === "rechazado" ||
      t.estado === "cancelado" ||
      t.estado === "error" ||
      (t.estado === "espera_humano" && !isLiveCaptcha);

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

        {!hideCategoryBadge && (
          <td className="px-4 py-3">
            <span className={`inline-flex items-center gap-1.5 rounded-lg border px-2 py-0.5 text-[11px] font-semibold ${catInfo.color}`}>
              <CatIcon className="h-3 w-3 shrink-0" />
              <span>{catInfo.label}</span>
            </span>
          </td>
        )}

        <td className="mono px-4 py-3 text-xs text-ink">
          {t.folio || "—"}
        </td>

        <td className="mono px-4 py-3 text-xs">
          <div className="font-bold text-ink">{formatCurrency(t.total)}</div>
          {t.desglose_impuestos && (
            <div className="text-[10px] text-muted flex flex-wrap gap-1">
              {(t.desglose_impuestos.iva_16 ?? 0) > 0 && <span>IVA 16%</span>}
              {(t.desglose_impuestos.base_0 ?? 0) > 0 && <span>0%</span>}
              {((t.desglose_impuestos.ieps ?? 0) > 0 || (t.desglose_impuestos.ish ?? 0) > 0) && (
                <span>IEPS/ISH</span>
              )}
            </div>
          )}
        </td>

        <td className="px-4 py-3">
          <div className="flex flex-col gap-1 items-start">
            {getDeducibilidadBadge(t.estatus_deducibilidad)}
            <div className="flex items-center gap-1.5 mt-0.5">
              {t.auditoria_aritmetica?.es_valido_anexo_20 && (
                <span className="inline-flex items-center gap-0.5 text-[10px] font-medium text-ok" title={`Score matemático Anexo 20: ${t.auditoria_aritmetica.score_matematico}/100`}>
                  <ShieldCheck className="h-2.5 w-2.5" />
                  <span>Anexo 20</span>
                </span>
              )}
              {t.score_riesgo_fiscal !== undefined && t.score_riesgo_fiscal !== null && (
                <span className={`inline-flex items-center px-1.5 py-0.2 text-[9px] font-semibold rounded ${
                  t.score_riesgo_fiscal <= 25
                    ? "bg-ok/10 text-ok"
                    : t.score_riesgo_fiscal <= 65
                    ? "bg-amber-500/10 text-amber-500"
                    : "bg-bad/10 text-bad"
                }`} title={`Riesgo Fiscal SAT Art. 69-B: ${t.score_riesgo_fiscal}/100`}>
                  Riesgo {t.score_riesgo_fiscal <= 25 ? "Bajo" : t.score_riesgo_fiscal <= 65 ? "Medio" : "Alto"}
                </span>
              )}
            </div>
          </div>
        </td>

        <td className="px-4 py-3">
          <StatusBadge
            estado={t.estado}
            errorCode={t.error_code}
            errorMsg={t.error_msg}
          />
        </td>

        <td className="px-4 py-3 text-xs">
          {imageEliminada ? (
            <span
              className="inline-flex items-center gap-1 text-[11px] text-muted cursor-help"
              title="Tu foto original fue procesada y eliminada para máxima privacidad de tus compras."
            >
              <Database className="h-3 w-3 text-muted/60" />
              <span>Eliminada</span>
            </span>
          ) : (
            <span
              className="inline-flex items-center gap-1 text-[11px] text-ok cursor-help"
              title="Foto y PDF escaneado disponibles mientras se procesa."
            >
              <Camera className="h-3 w-3 text-ok" />
              <span>HD Lista</span>
            </span>
          )}
        </td>

        <td className="px-4 py-3 text-xs text-muted">
          {formatDate(t.fecha_ticket || t.created_at)}
        </td>

        <td
          className="px-4 py-3 text-right"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-end gap-1.5">
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

            {isLiveCaptcha && (
              <button
                type="button"
                onClick={() => setSelectedTicketForHandoff(t)}
                title="Tomar control en vivo para resolver captcha"
                className="flex items-center gap-1 rounded-lg bg-amber-500 px-2 py-1 text-[11px] font-bold text-white shadow-sm hover:brightness-105 active:scale-95"
              >
                <Zap className="h-3 w-3" />
                <span>Resolver</span>
              </button>
            )}

            {isMissingPortal && (
              <button
                type="button"
                onClick={() => setSelectedTicketDetail(t)}
                title="Indicar o investigar portal de facturación con IA"
                className="flex items-center gap-1 rounded-lg bg-brand px-2 py-1 text-[11px] font-bold text-on-brand shadow-sm hover:brightness-105 active:scale-95"
              >
                <Globe className="h-3 w-3" />
                <span>Indicar portal</span>
              </button>
            )}

            {t.estado === "espera_humano" && !isLiveCaptcha && !isMissingPortal && (
              <button
                type="button"
                onClick={() => setSelectedTicketDetail(t)}
                title="Revisar portal o configurar reintento"
                className="flex items-center gap-1 rounded-lg border border-line-2 bg-panel-2 px-2 py-1 text-[11px] font-bold text-ink-2 shadow-sm hover:bg-line/20 active:scale-95"
              >
                <Globe className="h-3 w-3 text-muted" />
                <span>Revisar portal</span>
              </button>
            )}

            {t.estado === "facturado" && (
              <CfdiDownloadButton
                ticketId={t.id}
                disponibleHasta={t.cfdi_disponible_hasta}
                tenantId={tenantId}
                compact={true}
              />
            )}

            <button
              type="button"
              onClick={() => setSelectedTicketDetail(t)}
              title="Ver detalle del ticket"
              className="flex h-7 w-7 items-center justify-center rounded-lg border border-line-2 bg-panel text-muted hover:text-ink transition"
            >
              <Eye className="h-3.5 w-3.5" />
            </button>

            {canRetry && (
              <button
                type="button"
                onClick={() => handleRetry(t.id)}
                title="Reintentar facturación"
                className="flex h-7 w-7 items-center justify-center rounded-lg border border-line-2 bg-panel text-muted hover:text-brand transition hover:border-brand/40"
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </button>
            )}

            <button
              type="button"
              onClick={() => handleDelete(t.id)}
              title="Eliminar ticket"
              className="flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:text-bad transition"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </td>
      </tr>
    );
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Encabezado */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-ink">Tickets y Facturas</h1>
          <p className="mt-0.5 text-xs text-muted">
            {pendientesHumano > 0
              ? `${pendientesHumano} esperando algo tuyo`
              : "Todo en orden · Gastos divididos automáticamente por categoría SAT"}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => {
              setUploadModalTab("cfdi");
              setShowUploadModal(true);
            }}
            className="flex items-center gap-1.5 rounded-xl border border-line-2 bg-panel px-3.5 py-2.5 text-xs font-semibold text-ink shadow-sm hover:bg-panel-2 active:scale-95 transition"
          >
            <FileCode className="h-4 w-4 text-blue-400" />
            <span>Subir CFDI (XML)</span>
          </button>

          <button
            type="button"
            onClick={() => {
              setUploadModalTab("foto");
              setShowUploadModal(true);
            }}
            className="flex items-center gap-2 rounded-xl bg-brand px-4 py-2.5 text-xs font-bold text-on-brand shadow-sm transition-all hover:brightness-105 active:scale-95"
          >
            <Plus className="h-4 w-4 stroke-[2.5]" />
            <span>Subir tickets (Lote masivo)</span>
          </button>
        </div>
      </div>

      {/* Grid de 4 Indicadores (KPIs) - docs/prototipo.html:299-304 */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-xl border border-line bg-panel p-3.5 shadow-sm">
          <p className="lt">Este mes</p>
          <div className="mono mt-1 text-2xl font-bold text-ink">{totalMes}</div>
          <p className="mt-0.5 text-[11px] text-muted">comprobantes procesados</p>
        </div>

        <div className="rounded-xl border border-line bg-panel p-3.5 shadow-sm">
          <p className="lt">Clasificación</p>
          <div className="mono mt-1 text-2xl font-bold text-brand">
            100%
          </div>
          <p className="mt-0.5 text-[11px] text-muted">automática por SAT</p>
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

      {/* Franja de Resumen de División Automática y Selector de Vista */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-line bg-panel p-3.5 shadow-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 text-xs font-bold text-ink mr-1">
            <Layers className="h-4 w-4 text-brand" />
            <span>División de gastos:</span>
          </span>
          {categoriasConGastos.map((cat) => {
            const CatIcon = cat.icon;
            return (
              <div
                key={cat.id}
                className="flex items-center gap-1.5 rounded-lg border border-line-2 bg-panel-2 px-2.5 py-1 text-xs"
              >
                <CatIcon className="h-3.5 w-3.5 text-brand" />
                <span className="font-semibold text-ink">{cat.label}:</span>
                <span className="mono font-bold text-ink">{cat.tickets.length}</span>
                <span className="text-muted text-[11px]">({formatCurrency(cat.totalMonto)})</span>
              </div>
            );
          })}
          {categoriasConGastos.length === 0 && (
            <span className="text-muted text-xs">Sin comprobantes cargados en el mes.</span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-muted hidden sm:inline">Modo de visualización:</span>
          <div className="flex rounded-xl border border-line-2 bg-panel-2 p-1 text-xs">
            <button
              type="button"
              onClick={() => setViewMode("categorias")}
              className={`flex items-center gap-1.5 rounded-lg px-3 py-1 font-semibold transition ${
                viewMode === "categorias"
                  ? "bg-panel text-ink shadow-sm"
                  : "text-muted hover:text-ink"
              }`}
            >
              <Layers className="h-3.5 w-3.5 text-brand" />
              <span>Separado por categoría</span>
            </button>
            <button
              type="button"
              onClick={() => setViewMode("tabla")}
              className={`flex items-center gap-1.5 rounded-lg px-3 py-1 font-semibold transition ${
                viewMode === "tabla"
                  ? "bg-panel text-ink shadow-sm"
                  : "text-muted hover:text-ink"
              }`}
            >
              <Receipt className="h-3.5 w-3.5 text-muted" />
              <span>Tabla corrida</span>
            </button>
          </div>
        </div>
      </div>

      {/* Barra de Filtros de Estado y Categoría */}
      <div className="overflow-hidden rounded-2xl border border-line bg-panel shadow-prototipo">
        {/* Filtros de Estado */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-bold text-ink">
              {viewMode === "categorias" ? "Gastos separados por categoría" : "Cola unificada"}
            </h2>
            <span className="text-xs text-muted">
              ({filteredTickets.length} {filteredTickets.length === 1 ? "comprobante" : "comprobantes"})
            </span>
          </div>

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
              Todos los estados
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

        {/* Barra de Categorías Fiscales */}
        <div className="flex items-center gap-1.5 overflow-x-auto border-b border-line bg-panel-2/30 px-4 py-2 text-xs">
          <span className="text-[11px] font-semibold text-muted mr-1 hidden sm:inline shrink-0">Filtrar:</span>
          {CATEGORIAS.map((cat) => {
            const Icon = cat.icon;
            const isSelected = categoriaFilter === cat.id;
            return (
              <button
                key={cat.id}
                type="button"
                onClick={() => setCategoriaFilter(cat.id)}
                className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium whitespace-nowrap transition-all ${
                  isSelected
                    ? "bg-brand text-on-brand font-bold shadow-xs"
                    : "bg-panel text-ink-2 hover:bg-line/20 border border-line-2/60"
                }`}
              >
                <Icon className={`h-3.5 w-3.5 ${isSelected ? "text-on-brand" : "text-muted"}`} />
                <span>{cat.label}</span>
              </button>
            );
          })}
        </div>

        {/* CONTENIDO PRINCIPAL: VISTA DE CATEGORÍAS SEPARADAS (DEFAULT) */}
        {viewMode === "categorias" ? (
          <div className="p-4 space-y-6">
            {categoriasConGastos.map((cat) => {
              const CatIcon = cat.icon;
              return (
                <div
                  key={cat.id}
                  className="overflow-hidden rounded-2xl border border-line bg-panel shadow-sm"
                >
                  {/* Cabecera de Categoría Separada */}
                  <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-panel-2/40 px-5 py-3.5">
                    <div className="flex items-center gap-3">
                      <div className={`flex h-9 w-9 items-center justify-center rounded-xl border ${cat.color}`}>
                        <CatIcon className="h-4 w-4" />
                      </div>
                      <div>
                        <div className="flex items-center gap-2">
                          <h3 className="text-sm font-bold text-ink">{cat.label}</h3>
                          <span className="rounded-full bg-brand/10 text-brand border border-brand/20 px-2 py-0.5 text-[10px] font-bold">
                            {cat.tickets.length} {cat.tickets.length === 1 ? "comprobante" : "comprobantes"}
                          </span>
                        </div>
                        <p className="text-[11px] text-muted">{cat.satRule}</p>
                      </div>
                    </div>

                    <div className="flex items-center gap-3">
                      <div className="text-right">
                        <span className="text-[10px] uppercase font-semibold text-muted block">Subtotal categoría</span>
                        <span className="mono text-sm font-bold text-ink">{formatCurrency(cat.totalMonto)}</span>
                      </div>
                    </div>
                  </div>

                  {/* Tabla Exclusiva de esta Categoría */}
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs">
                      <thead>
                        <tr className="border-b border-line bg-panel-2/20 text-[10px] font-semibold uppercase tracking-wider text-muted">
                          <th className="px-4 py-2.5">Comercio</th>
                          <th className="px-4 py-2.5">Folio</th>
                          <th className="px-4 py-2.5">Monto / Impuestos</th>
                          <th className="px-4 py-2.5">Deducibilidad SAT</th>
                          <th className="px-4 py-2.5">Estado</th>
                          <th className="px-4 py-2.5">Imagen</th>
                          <th className="px-4 py-2.5">Fecha</th>
                          <th className="px-4 py-2.5 text-right">Acción</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-line">
                        {cat.tickets.map((t) => renderTicketRow(t, true))}
                      </tbody>
                    </table>
                  </div>
                </div>
              );
            })}

            {/* Categorías sin tickets para transparencia del sistema */}
            {categoriasVacias.length > 0 && categoriaFilter === "todos" && (
              <div className="rounded-2xl border border-line/60 bg-panel/40 p-4">
                <h4 className="text-xs font-bold uppercase tracking-wider text-muted mb-3 flex items-center gap-1.5">
                  <Layers className="h-3.5 w-3.5" />
                  <span>Otras categorías listas para recibir comprobantes (0 gastos):</span>
                </h4>
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2.5">
                  {categoriasVacias.map((cat) => {
                    const CatIcon = cat.icon;
                    return (
                      <div
                        key={cat.id}
                        className="flex items-center gap-2.5 rounded-xl border border-line-2/70 bg-panel p-2.5 text-xs text-muted hover:border-brand/40 transition cursor-pointer"
                        onClick={() => {
                          setUploadModalTab("cfdi");
                          setShowUploadModal(true);
                        }}
                      >
                        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-panel-2 text-muted">
                          <CatIcon className="h-3.5 w-3.5" />
                        </div>
                        <div className="flex-1 truncate">
                          <span className="font-semibold text-ink-2 block truncate">{cat.label}</span>
                          <span className="text-[10px] text-muted">$0.00 · Clic para subir</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Estado Vacío */}
            {!loading && filteredTickets.length === 0 && (
              <div className="py-12 text-center">
                <div className="mx-auto flex max-w-xs flex-col items-center gap-2 text-muted">
                  <Receipt className="h-8 w-8 stroke-[1.5] text-line-2" />
                  <b className="text-sm font-semibold text-ink">
                    No hay tickets registrados
                  </b>
                  <p className="text-xs">
                    Presiona &ldquo;Subir ticket&rdquo; o &ldquo;Subir CFDI&rdquo; para procesar tu primer comprobante.
                  </p>
                  <div className="mt-2 flex items-center justify-center gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setUploadModalTab("foto");
                        setShowUploadModal(true);
                      }}
                      className="rounded-xl bg-brand px-3 py-1.5 text-xs font-bold text-on-brand shadow hover:brightness-105"
                    >
                      ＋ Subir ticket
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setUploadModalTab("cfdi");
                        setShowUploadModal(true);
                      }}
                      className="rounded-xl border border-line-2 bg-panel px-3 py-1.5 text-xs font-semibold text-ink hover:bg-panel-2"
                    >
                      📄 Subir CFDI
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        ) : (
          /* TABLA CORRIDA UNIFICADA */
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-line bg-panel-2/50 text-[10px] font-semibold uppercase tracking-wider text-muted">
                  <th className="px-4 py-2.5">Comercio</th>
                  <th className="px-4 py-2.5">Categoría</th>
                  <th className="px-4 py-2.5">Folio</th>
                  <th className="px-4 py-2.5">Monto</th>
                  <th className="px-4 py-2.5">Deducibilidad SAT</th>
                  <th className="px-4 py-2.5">Estado</th>
                  <th className="px-4 py-2.5">Imagen</th>
                  <th className="px-4 py-2.5">Fecha</th>
                  <th className="px-4 py-2.5 text-right">Acción</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {filteredTickets.map((t) => renderTicketRow(t, false))}

                {!loading && filteredTickets.length === 0 && (
                  <tr>
                    <td colSpan={9} className="py-12 text-center">
                      <div className="mx-auto flex max-w-xs flex-col items-center gap-2 text-muted">
                        <Receipt className="h-8 w-8 stroke-[1.5] text-line-2" />
                        <b className="text-sm font-semibold text-ink">
                          No hay tickets registrados
                        </b>
                        <p className="text-xs">
                          Presiona &ldquo;Subir ticket&rdquo; o &ldquo;Subir CFDI&rdquo; para procesar tu primer comprobante.
                        </p>
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* MODAL: SUBIR TICKET O CFDI */}
      {showUploadModal && tenantId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-lg rounded-2xl border border-line bg-panel p-5 shadow-2xl">
            <div className="mb-4 flex items-start justify-between">
              <div>
                <h3 className="text-base font-bold text-ink">Ingresar comprobante fiscal</h3>
                <p className="text-xs text-muted">
                  Fotografía un ticket de compra o sube una factura CFDI XML directa
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

            {/* Selector de Pestañas Foto vs CFDI */}
            <div className="mb-5 flex rounded-xl border border-line-2 bg-panel-2 p-1 text-xs">
              <button
                type="button"
                onClick={() => setUploadModalTab("foto")}
                className={`flex flex-1 items-center justify-center gap-2 rounded-lg py-2 font-semibold transition ${
                  uploadModalTab === "foto"
                    ? "bg-panel text-ink shadow-sm"
                    : "text-muted hover:text-ink"
                }`}
              >
                <Camera className="h-3.5 w-3.5 text-brand" />
                <span>📸 Escaneo CamScanner HD (Individual o Lote)</span>
              </button>
              <button
                type="button"
                onClick={() => setUploadModalTab("cfdi")}
                className={`flex flex-1 items-center justify-center gap-2 rounded-lg py-2 font-semibold transition ${
                  uploadModalTab === "cfdi"
                    ? "bg-panel text-ink shadow-sm"
                    : "text-muted hover:text-ink"
                }`}
              >
                <FileCode className="h-3.5 w-3.5 text-blue-400" />
                <span>Subir CFDI (XML / PDF)</span>
              </button>
            </div>

            {uploadModalTab === "foto" ? (
              <MobileCameraUpload
                tenantId={tenantId}
                onSuccess={() => {
                  loadTickets();
                }}
                onClose={() => setShowUploadModal(false)}
              />
            ) : (
              <DirectCfdiUpload
                tenantId={tenantId}
                onSuccess={() => {
                  loadTickets();
                }}
                onClose={() => setShowUploadModal(false)}
              />
            )}
          </div>
        </div>
      )}

      {/* MODAL: INTERVENCIÓN HUMANA EN VIVO (Handoff interactivo) */}
      {selectedTicketForHandoff && tenantId && (
        <HandoffModal
          tenantId={tenantId}
          ticketId={selectedTicketForHandoff.id}
          onClose={() => {
            setSelectedTicketForHandoff(null);
            loadTickets();
          }}
          onSuccess={() => {
            setSelectedTicketForHandoff(null);
            loadTickets();
          }}
          onRetry={() => {
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
          onTicketUpdated={(updatedTicket) => {
            setSelectedTicketDetail(updatedTicket);
            setTickets((prev) =>
              prev.map((item) => (item.id === updatedTicket.id ? updatedTicket : item))
            );
          }}
        />
      )}
    </div>
  );
}
