"use client";

import React, { useRef, useState } from "react";
import {
  FileCode,
  FileText,
  UploadCloud,
  CheckCircle2,
  AlertCircle,
  ShieldCheck,
  ShieldAlert,
  Fuel,
  Hotel,
  Utensils,
  ShoppingCart,
  Navigation,
  Plane,
  X,
  ArrowRight,
  Loader2,
} from "lucide-react";
import { uploadCfdiDirect } from "@/lib/api";
import { TicketResponse } from "@/types/api";
import { formatCurrency } from "@/lib/utils";

interface DirectCfdiUploadProps {
  tenantId: string;
  onSuccess?: (ticket: TicketResponse) => void;
  onClose?: () => void;
}

export function DirectCfdiUpload({
  tenantId,
  onSuccess,
  onClose,
}: DirectCfdiUploadProps) {
  const xmlInputRef = useRef<HTMLInputElement>(null);
  const pdfInputRef = useRef<HTMLInputElement>(null);

  const [xmlFile, setXmlFile] = useState<File | null>(null);
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [resultTicket, setResultTicket] = useState<TicketResponse | null>(null);

  const handleXmlChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".xml")) {
      setErrorMsg("El archivo principal debe ser un XML fiscal válido (CFDI 3.3 o 4.0).");
      return;
    }
    setErrorMsg(null);
    setXmlFile(file);
  };

  const handlePdfChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setErrorMsg("El archivo complementario debe ser un PDF.");
      return;
    }
    setErrorMsg(null);
    setPdfFile(file);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();

    const files = Array.from(e.dataTransfer.files);
    let foundXml: File | null = null;
    let foundPdf: File | null = null;

    for (const f of files) {
      if (f.name.toLowerCase().endsWith(".xml")) {
        foundXml = f;
      } else if (f.name.toLowerCase().endsWith(".pdf")) {
        foundPdf = f;
      }
    }

    if (foundXml) setXmlFile(foundXml);
    if (foundPdf) setPdfFile(foundPdf);

    if (!foundXml && !xmlFile) {
      setErrorMsg("Por favor arrastra al menos un archivo .XML del CFDI.");
    } else {
      setErrorMsg(null);
    }
  };

  const handleUpload = async () => {
    if (!xmlFile) {
      setErrorMsg("Debes seleccionar el archivo XML del CFDI.");
      return;
    }

    setUploading(true);
    setErrorMsg(null);

    try {
      const ticket = await uploadCfdiDirect(tenantId, xmlFile, pdfFile || undefined);
      setResultTicket(ticket);
      if (onSuccess) {
        onSuccess(ticket);
      }
    } catch (err: unknown) {
      setErrorMsg((err as Error).message || "Error al procesar el comprobante CFDI.");
    } finally {
      setUploading(false);
    }
  };

  const handleReset = () => {
    setXmlFile(null);
    setPdfFile(null);
    setResultTicket(null);
    setErrorMsg(null);
    if (xmlInputRef.current) xmlInputRef.current.value = "";
    if (pdfInputRef.current) pdfInputRef.current.value = "";
  };

  return (
    <div className="w-full">
      <input
        ref={xmlInputRef}
        type="file"
        accept=".xml,text/xml,application/xml"
        onChange={handleXmlChange}
        className="hidden"
      />
      <input
        ref={pdfInputRef}
        type="file"
        accept=".pdf,application/pdf"
        onChange={handlePdfChange}
        className="hidden"
      />

      {/* Vista de Carga */}
      {!resultTicket && (
        <div className="flex flex-col gap-4">
          <div
            onDragOver={(e) => {
              e.preventDefault();
              e.stopPropagation();
            }}
            onDrop={handleDrop}
            className="flex flex-col items-center justify-center rounded-2xl border-2 border-dashed border-line-2 bg-panel-2 p-6 text-center transition-all hover:border-brand hover:bg-brand-soft/40"
          >
            <div className="mb-3 flex h-14 w-14 items-center justify-center rounded-2xl bg-brand/10 text-brand shadow-sm">
              <UploadCloud className="h-7 w-7" />
            </div>

            <b className="text-base font-bold text-ink">
              Arrastra tu factura CFDI (.XML y .PDF)
            </b>
            <p className="mt-1 max-w-sm text-xs text-muted">
              Detecta automáticamente el RFC emisor, desglose de IVA 16%, Tasa 0%, Exento, IEPS, ISH y evalúa deducibilidad SAT (Art. 27 y 28 LISR).
            </p>

            <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
              <button
                type="button"
                onClick={() => xmlInputRef.current?.click()}
                className="flex items-center gap-1.5 rounded-xl border border-line-2 bg-panel px-3.5 py-2 text-xs font-semibold text-ink shadow-sm hover:bg-panel-2"
              >
                <FileCode className="h-4 w-4 text-blue-400" />
                <span>{xmlFile ? xmlFile.name : "Seleccionar XML (Obligatorio)"}</span>
              </button>

              <button
                type="button"
                onClick={() => pdfInputRef.current?.click()}
                className="flex items-center gap-1.5 rounded-xl border border-line-2 bg-panel px-3.5 py-2 text-xs font-semibold text-ink shadow-sm hover:bg-panel-2"
              >
                <FileText className="h-4 w-4 text-red-400" />
                <span>{pdfFile ? pdfFile.name : "Seleccionar PDF (Opcional)"}</span>
              </button>
            </div>
          </div>

          {/* Resumen de archivos seleccionados */}
          {(xmlFile || pdfFile) && (
            <div className="rounded-xl border border-line bg-panel p-3.5 flex flex-col gap-2 text-xs">
              <div className="flex items-center justify-between font-semibold text-ink">
                <span>Archivos listos para procesar:</span>
                <button
                  type="button"
                  onClick={handleReset}
                  className="text-muted hover:text-bad text-[11px]"
                >
                  Limpiar
                </button>
              </div>
              {xmlFile && (
                <div className="flex items-center justify-between text-muted">
                  <span className="flex items-center gap-1.5 text-blue-400 font-medium">
                    <FileCode className="h-3.5 w-3.5" />
                    {xmlFile.name}
                  </span>
                  <span className="mono">{Math.round(xmlFile.size / 1024)} KB</span>
                </div>
              )}
              {pdfFile && (
                <div className="flex items-center justify-between text-muted">
                  <span className="flex items-center gap-1.5 text-red-400 font-medium">
                    <FileText className="h-3.5 w-3.5" />
                    {pdfFile.name}
                  </span>
                  <span className="mono">{Math.round(pdfFile.size / 1024)} KB</span>
                </div>
              )}
            </div>
          )}

          {errorMsg && (
            <div className="flex items-center gap-2 rounded-xl border border-bad/30 bg-bad-soft p-3 text-xs font-medium text-bad">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>{errorMsg}</span>
            </div>
          )}

          <div className="flex items-center justify-end gap-3 pt-2">
            {onClose && (
              <button
                type="button"
                onClick={onClose}
                className="rounded-xl border border-line-2 bg-panel px-4 py-2.5 text-xs font-semibold text-ink hover:bg-panel-2"
              >
                Cancelar
              </button>
            )}
            <button
              type="button"
              disabled={!xmlFile || uploading}
              onClick={handleUpload}
              className="flex items-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-xs font-bold text-on-brand shadow hover:brightness-105 disabled:opacity-50 active:scale-95 transition"
            >
              {uploading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>Analizando CFDI...</span>
                </>
              ) : (
                <>
                  <span>Clasificar y Registrar CFDI</span>
                  <ArrowRight className="h-4 w-4" />
                </>
              )}
            </button>
          </div>
        </div>
      )}

      {/* Vista de Éxito / Resultado Inmediato */}
      {resultTicket && (
        <div className="flex flex-col gap-4 animate-in fade-in duration-200">
          <div className="rounded-xl border border-ok/30 bg-ok-soft/30 p-4">
            <div className="flex items-center gap-2.5">
              <CheckCircle2 className="h-5 w-5 text-ok shrink-0" />
              <div>
                <b className="text-sm font-bold text-ink">CFDI 4.0 Clasificado e Importado con Éxito</b>
                <p className="text-xs text-muted">
                  RFC: <span className="mono font-semibold text-ink">{resultTicket.rfc_emisor}</span> · Total: <span className="mono font-bold text-ink">{formatCurrency(resultTicket.total)}</span>
                </p>
              </div>
            </div>
          </div>

          {/* Fila de Categoría y Deducibilidad */}
          <div className="flex items-center justify-between rounded-xl border border-line bg-panel p-3.5 text-xs">
            <div className="flex items-center gap-2">
              <span className="text-muted font-medium">Categoría asignada:</span>
              <span className="font-bold text-ink flex items-center gap-1.5">
                {resultTicket.categoria_gasto === "combustible" && <Fuel className="h-4 w-4 text-amber-500" />}
                {resultTicket.categoria_gasto === "hospedaje" && <Hotel className="h-4 w-4 text-blue-500" />}
                {resultTicket.categoria_gasto === "restaurante" && <Utensils className="h-4 w-4 text-orange-500" />}
                {resultTicket.categoria_gasto === "supermercado" && <ShoppingCart className="h-4 w-4 text-emerald-500" />}
                {resultTicket.categoria_gasto === "casetas_peaje" && <Navigation className="h-4 w-4 text-indigo-500" />}
                {resultTicket.categoria_gasto === "vuelos_transporte" && <Plane className="h-4 w-4 text-cyan-500" />}
                <span>
                  {resultTicket.categoria_gasto === "combustible" ? "Combustible" :
                   resultTicket.categoria_gasto === "hospedaje" ? "Hospedaje" :
                   resultTicket.categoria_gasto === "restaurante" ? "Restaurante" :
                   resultTicket.categoria_gasto === "supermercado" ? "Supermercado" :
                   resultTicket.categoria_gasto === "casetas_peaje" ? "Caseta Peaje" :
                   resultTicket.categoria_gasto === "vuelos_transporte" ? "Vuelo / Transporte" : "Servicios"}
                </span>
              </span>
            </div>

            {resultTicket.estatus_deducibilidad && (
              <span className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold ${
                resultTicket.estatus_deducibilidad === "deducible_100" ? "bg-ok-soft text-ok border border-ok/30" :
                resultTicket.estatus_deducibilidad === "no_deducible" ? "bg-bad/15 text-bad border border-bad/30" :
                "bg-amber-500/15 text-amber-400 border border-amber-500/30"
              }`}>
                {resultTicket.estatus_deducibilidad === "deducible_100" ? <ShieldCheck className="h-3.5 w-3.5" /> : <ShieldAlert className="h-3.5 w-3.5" />}
                <span>
                  {resultTicket.estatus_deducibilidad === "deducible_100" ? "100% Deducible" :
                   resultTicket.estatus_deducibilidad === "no_deducible" ? "No Deducible (Efectivo)" :
                   resultTicket.estatus_deducibilidad === "deducible_parcial" ? "8.5% Art. 28 LISR" : "Condicionado"}
                </span>
              </span>
            )}
          </div>

          {/* Desglose de Impuestos */}
          {resultTicket.desglose_impuestos && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
              <div className="rounded-lg bg-panel p-2.5 border border-line">
                <span className="text-[10px] text-muted block">IVA 16%</span>
                <span className="mono font-bold text-ink">{formatCurrency(resultTicket.desglose_impuestos.iva_16 ?? 0)}</span>
              </div>
              <div className="rounded-lg bg-panel p-2.5 border border-line">
                <span className="text-[10px] text-muted block">Tasa 0%</span>
                <span className="mono font-bold text-ink">{formatCurrency(resultTicket.desglose_impuestos.base_0 ?? 0)}</span>
              </div>
              <div className="rounded-lg bg-panel p-2.5 border border-line">
                <span className="text-[10px] text-muted block">IEPS / ISH</span>
                <span className="mono font-bold text-ink">
                  {formatCurrency((resultTicket.desglose_impuestos.ieps ?? 0) + (resultTicket.desglose_impuestos.ish ?? 0))}
                </span>
              </div>
              <div className="rounded-lg bg-panel p-2.5 border border-line">
                <span className="text-[10px] text-muted block">Retenciones</span>
                <span className="mono font-bold text-bad">
                  -{formatCurrency((resultTicket.desglose_impuestos.retencion_iva ?? 0) + (resultTicket.desglose_impuestos.retencion_isr ?? 0))}
                </span>
              </div>
            </div>
          )}

          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={handleReset}
              className="rounded-xl border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink hover:bg-panel-2"
            >
              Subir otro CFDI
            </button>
            {onClose && (
              <button
                type="button"
                onClick={onClose}
                className="rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand hover:brightness-105"
              >
                Listo, ver en tabla
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
