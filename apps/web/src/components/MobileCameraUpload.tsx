"use client";

import React, { useRef, useState } from "react";
import {
  Camera,
  Upload,
  X,
  AlertTriangle,
  ArrowRight,
  Plus,
  Copy,
  CheckCircle2,
  Loader2,
  Trash2,
  Sparkles,
  FileText,
} from "lucide-react";
import { uploadTicket, uploadTicketsBatch } from "@/lib/api";
import { TicketStreamLog } from "./TicketStreamLog";
import { TicketResponse } from "@/types/api";
import { cn } from "@/lib/utils";

interface BatchItem {
  id: string; // client id
  file: File;
  name: string;
  size: number;
  previewUrl: string | null;
  isDuplicateInBatch: boolean;
}

interface ProcessedTicketStatus {
  ticketId: string;
  name: string;
  estado: string;
  isDuplicate: boolean;
  errorMsg?: string | null;
  ticket?: TicketResponse;
}

interface MobileCameraUploadProps {
  tenantId: string;
  onSuccess?: () => void;
  onClose?: () => void;
}

export function MobileCameraUpload({
  tenantId,
  onSuccess,
  onClose,
}: MobileCameraUploadProps) {
  const cameraInputRef = useRef<HTMLInputElement>(null);
  const galleryInputRef = useRef<HTMLInputElement>(null);

  const [batchItems, setBatchItems] = useState<BatchItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgressText, setUploadProgressText] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Resultados del procesamiento
  const [processedResults, setProcessedResults] = useState<ProcessedTicketStatus[]>([]);
  const [activeTicketId, setActiveTicketId] = useState<string | null>(null);

  // Advertencia de fecha de mes anterior
  const [showMonthWarning, setShowMonthWarning] = useState(false);
  const [confirmedMonthWarning, setConfirmedMonthWarning] = useState(false);

  // Compresión en el cliente para subida instantánea en redes móviles
  const compressImageIfNeeded = async (file: File): Promise<File> => {
    if (!file.type.match(/^image\/(jpeg|png|webp)$/i) && !file.name.match(/\.(jpe?g|png|webp)$/i)) {
      return file;
    }
    if (file.size < 800 * 1024) {
      return file;
    }

    return new Promise((resolve) => {
      const img = new Image();
      const objectUrl = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(objectUrl);
        const maxDim = 1920;
        let width = img.width;
        let height = img.height;

        if (width > maxDim || height > maxDim) {
          if (width > height) {
            height = Math.round((height * maxDim) / width);
            width = maxDim;
          } else {
            width = Math.round((width * maxDim) / height);
            height = maxDim;
          }
        }

        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          resolve(file);
          return;
        }
        ctx.drawImage(img, 0, 0, width, height);

        canvas.toBlob(
          (blob) => {
            if (blob && blob.size < file.size) {
              const compressed = new File(
                [blob],
                file.name.replace(/\.[^.]+$/, ".jpg"),
                { type: "image/jpeg", lastModified: Date.now() }
              );
              resolve(compressed);
            } else {
              resolve(file);
            }
          },
          "image/jpeg",
          0.85
        );
      };
      img.onerror = () => {
        URL.revokeObjectURL(objectUrl);
        resolve(file);
      };
      img.src = objectUrl;
    });
  };

  const handleFilesAdded = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;

    setErrorMsg(null);
    const newItems: BatchItem[] = [];
    const existingSignatures = new Set(
      batchItems.map((item) => `${item.name}-${item.size}`)
    );

    for (let i = 0; i < fileList.length; i++) {
      const rawFile = fileList[i];
      if (rawFile.size > 20 * 1024 * 1024) {
        setErrorMsg(`El archivo "${rawFile.name}" excede el tamaño máximo permitido de 20 MB.`);
        continue;
      }

      let processedFile = rawFile;
      try {
        processedFile = await compressImageIfNeeded(rawFile);
      } catch {
        // Fallback al original
      }

      const signature = `${processedFile.name}-${processedFile.size}`;
      const isDup = existingSignatures.has(signature);
      existingSignatures.add(signature);

      let previewUrl: string | null = null;
      if (
        processedFile.type.startsWith("image/") ||
        processedFile.name.match(/\.(jpe?g|png|webp|heic|heif)$/i)
      ) {
        previewUrl = URL.createObjectURL(processedFile);
      }

      newItems.push({
        id: Math.random().toString(36).substring(2, 9),
        file: processedFile,
        name: processedFile.name,
        size: processedFile.size,
        previewUrl,
        isDuplicateInBatch: isDup,
      });
    }

    setBatchItems((prev) => [...prev, ...newItems]);
  };

  const handleRemoveItem = (id: string) => {
    setBatchItems((prev) => {
      const filtered = prev.filter((it) => it.id !== id);
      // Recalcular duplicados en lote
      const sigs = new Set<string>();
      return filtered.map((it) => {
        const sig = `${it.name}-${it.size}`;
        const isDup = sigs.has(sig);
        sigs.add(sig);
        return { ...it, isDuplicateInBatch: isDup };
      });
    });
  };

  const handleStartProcessing = async () => {
    if (batchItems.length === 0) return;

    setUploading(true);
    setErrorMsg(null);
    setUploadProgressText("Optimizando y enviando tickets al servidor...");

    try {
      const filesToUpload = batchItems.map((it) => it.file);

      // Usar endpoint de lote para procesamiento masivo concurrente
      const resp = await uploadTicketsBatch(tenantId, filesToUpload);

      const mappedStatuses: ProcessedTicketStatus[] = resp.items.map((t, idx) => {
        const originalName = batchItems[idx]?.name || `Ticket ${idx + 1}`;
        const isDup = t.error_code === "duplicado" || (t.error_msg && t.error_msg.toLowerCase().includes("duplicad")) || false;
        return {
          ticketId: t.id,
          name: originalName,
          estado: t.estado,
          isDuplicate: isDup,
          errorMsg: t.error_msg,
          ticket: t,
        };
      });

      setProcessedResults(mappedStatuses);

      // Seleccionar el primer ticket no duplicado (o el primero) para ver su stream
      const firstValid = mappedStatuses.find((m) => !m.isDuplicate) || mappedStatuses[0];
      if (firstValid) {
        setActiveTicketId(firstValid.ticketId);
      }

      if (onSuccess) {
        onSuccess();
      }
    } catch (err: unknown) {
      setErrorMsg(
        (err as Error).message || "Ocurrió un error al subir el lote de tickets."
      );
    } finally {
      setUploading(false);
      setUploadProgressText(null);
    }
  };

  const handleReset = () => {
    batchItems.forEach((it) => {
      if (it.previewUrl) URL.revokeObjectURL(it.previewUrl);
    });
    setBatchItems([]);
    setUploading(false);
    setUploadProgressText(null);
    setProcessedResults([]);
    setActiveTicketId(null);
    setErrorMsg(null);
    setShowMonthWarning(false);
    setConfirmedMonthWarning(false);
    if (cameraInputRef.current) cameraInputRef.current.value = "";
    if (galleryInputRef.current) galleryInputRef.current.value = "";
  };

  const countDuplicates = batchItems.filter((i) => i.isDuplicateInBatch).length;

  return (
    <div className="w-full">
      {/* Inputs ocultos: Cámara directa y Galería múltiple */}
      <input
        ref={cameraInputRef}
        type="file"
        accept="image/*,.heic,.heif"
        capture="environment"
        onChange={(e) => {
          handleFilesAdded(e.target.files);
          if (cameraInputRef.current) cameraInputRef.current.value = "";
        }}
        className="hidden"
      />
      <input
        ref={galleryInputRef}
        type="file"
        accept="image/*,.heic,.heif,application/pdf"
        multiple
        onChange={(e) => {
          handleFilesAdded(e.target.files);
          if (galleryInputRef.current) galleryInputRef.current.value = "";
        }}
        className="hidden"
      />

      {/* VISTA 1: Selector Inicial (Cámara vs Subida Masiva / Galería) */}
      {batchItems.length === 0 && processedResults.length === 0 && (
        <div className="flex flex-col gap-3">
          {/* Botón Principal: Subida Masiva / Galería Múltiple */}
          <div
            onClick={() => galleryInputRef.current?.click()}
            className="group flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed border-line-2 bg-panel-2 p-7 text-center transition-all hover:border-brand hover:bg-brand-soft active:scale-[0.99]"
          >
            <div className="mb-3 flex h-14 w-14 items-center justify-center rounded-2xl bg-brand text-on-brand shadow-lg transition-transform group-hover:scale-105">
              <Upload className="h-7 w-7 stroke-[2.2]" />
            </div>

            <b className="text-base font-bold text-ink">
              Subir múltiples tickets a la vez
            </b>
            <span className="mt-1 text-xs text-muted max-w-xs">
              Selecciona fotos de tu galería o archivos PDF. Procesamiento masivo con IA y detección de duplicados.
            </span>

            <div className="mt-4 flex items-center gap-2 rounded-full border border-line-2 bg-panel px-4 py-1.5 text-xs font-semibold text-ink shadow-sm">
              <Sparkles className="h-3.5 w-3.5 text-brand" />
              <span>Elegir varios tickets a la vez</span>
            </div>
          </div>

          {/* Botón Secundario: Tomar Foto Directa con Cámara */}
          <button
            type="button"
            onClick={() => cameraInputRef.current?.click()}
            className="flex items-center justify-center gap-2.5 rounded-xl border border-line-2 bg-panel py-3 px-4 text-xs font-bold text-ink transition-all hover:bg-panel-2 active:scale-[0.99]"
          >
            <Camera className="h-4 w-4 text-brand stroke-[2.2]" />
            <span>Tomar foto con la cámara</span>
          </button>
        </div>
      )}

      {/* VISTA 2: Revisión de Lote, Detección de Duplicados y Confirmación */}
      {batchItems.length > 0 && processedResults.length === 0 && (
        <div className="flex flex-col gap-4">
          {/* Cabecera del Lote */}
          <div className="flex items-center justify-between border-b border-line pb-3">
            <div>
              <div className="flex items-center gap-2">
                <b className="text-sm font-bold text-ink">
                  {batchItems.length === 1
                    ? "1 ticket preparado"
                    : `Lote de ${batchItems.length} tickets listos`}
                </b>
                <span className="rounded-full bg-brand-soft px-2.5 py-0.5 text-[11px] font-bold text-brand">
                  Masivo
                </span>
              </div>
              <p className="text-xs text-muted">
                Revisa tus comprobantes antes de iniciar el procesamiento concurrente
              </p>
            </div>

            <button
              type="button"
              onClick={handleReset}
              className="text-xs font-semibold text-muted hover:text-bad transition-colors"
            >
              Borrar todo
            </button>
          </div>

          {/* Alerta de Duplicados en el Lote */}
          {countDuplicates > 0 && (
            <div className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-600 dark:text-amber-400">
              <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
              <div>
                <b className="font-bold">
                  {countDuplicates === 1
                    ? "1 ticket duplicado detectado en la selección"
                    : `${countDuplicates} tickets duplicados detectados`}
                </b>
                <p className="mt-0.5 text-[11px] opacity-90">
                  Los comprobantes idénticos están marcados con una insignia. El servidor los filtrará automáticamente para no consumir recursos innecesarios.
                </p>
              </div>
            </div>
          )}

          {/* Cuadrícula / Lista de Tickets en el Lote */}
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5 max-h-80 overflow-y-auto pr-1">
            {batchItems.map((item) => (
              <div
                key={item.id}
                className={cn(
                  "relative group flex flex-col rounded-xl border bg-panel-2 p-2 overflow-hidden transition-all",
                  item.isDuplicateInBatch
                    ? "border-amber-500/50 bg-amber-500/5"
                    : "border-line hover:border-brand/40"
                )}
              >
                {/* Miniatura / Preview */}
                <div className="relative aspect-[3/4] w-full rounded-lg overflow-hidden bg-black/5 flex items-center justify-center">
                  {item.previewUrl ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={item.previewUrl}
                      alt={item.name}
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <div className="flex flex-col items-center gap-1 text-muted">
                      <FileText className="h-8 w-8 stroke-[1.5]" />
                      <span className="text-[10px] font-semibold">PDF</span>
                    </div>
                  )}

                  {/* Insignia de duplicado */}
                  {item.isDuplicateInBatch && (
                    <div className="absolute top-1.5 left-1.5 flex items-center gap-1 rounded-md bg-amber-500 px-1.5 py-0.5 text-[9px] font-bold text-white shadow">
                      <Copy className="h-2.5 w-2.5" />
                      <span>Duplicado</span>
                    </div>
                  )}

                  {/* Botón Quitar */}
                  <button
                    type="button"
                    onClick={() => handleRemoveItem(item.id)}
                    className="absolute top-1.5 right-1.5 flex h-6 w-6 items-center justify-center rounded-full bg-black/65 text-white backdrop-blur-sm transition-opacity hover:bg-black/90"
                    aria-label="Quitar de la lista"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>

                {/* Info Archivo */}
                <div className="mt-1.5 flex flex-col">
                  <span className="text-[11px] font-semibold text-ink truncate">
                    {item.name}
                  </span>
                  <span className="text-[10px] text-muted">
                    {Math.round(item.size / 1024)} KB
                  </span>
                </div>
              </div>
            ))}

            {/* Tarjeta para agregar más */}
            <div
              onClick={() => galleryInputRef.current?.click()}
              className="flex aspect-[3/4] cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed border-line-2 bg-panel p-2 text-center transition-all hover:border-brand hover:bg-brand-soft/50"
            >
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-panel-2 text-brand mb-1">
                <Plus className="h-4 w-4" />
              </div>
              <span className="text-[11px] font-bold text-ink">Agregar más</span>
              <span className="text-[9px] text-muted">Fotos o PDFs</span>
            </div>
          </div>

          {/* Botones de Acción */}
          <div className="flex items-center gap-2 pt-2">
            <button
              type="button"
              onClick={() => cameraInputRef.current?.click()}
              className="flex items-center gap-1.5 rounded-xl border border-line-2 bg-panel px-3.5 py-2.5 text-xs font-semibold text-ink hover:bg-panel-2 transition-colors"
            >
              <Camera className="h-3.5 w-3.5 text-brand" />
              <span>Cámara</span>
            </button>

            <button
              type="button"
              disabled={uploading || batchItems.length === 0}
              onClick={handleStartProcessing}
              className={cn(
                "flex flex-1 items-center justify-center gap-2 rounded-xl bg-brand py-3 text-xs font-bold text-on-brand shadow-md transition-all hover:brightness-110 active:scale-[0.98]",
                uploading && "opacity-70 cursor-not-allowed"
              )}
            >
              {uploading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>{uploadProgressText || "Procesando en masa..."}</span>
                </>
              ) : (
                <>
                  <span>
                    Procesar lote ({batchItems.length}{" "}
                    {batchItems.length === 1 ? "ticket" : "tickets"}) en masa
                  </span>
                  <ArrowRight className="h-4 w-4" />
                </>
              )}
            </button>
          </div>
        </div>
      )}

      {/* VISTA 3: Lote en Proceso Masivo Concurrente con Visor Individual */}
      {processedResults.length > 0 && (
        <div className="flex flex-col gap-4">
          {/* Resumen del Lote */}
          <div className="rounded-xl border border-line bg-panel p-3.5 shadow-sm">
            <div className="flex items-center justify-between border-b border-line pb-2.5 mb-2.5">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-brand" />
                <b className="text-sm font-bold text-ink">
                  Procesando {processedResults.length}{" "}
                  {processedResults.length === 1 ? "ticket" : "tickets"} en paralelo
                </b>
              </div>
              <span className="text-[11px] font-semibold text-muted">
                {processedResults.filter((r) => r.isDuplicate).length > 0 && (
                  <span className="text-amber-500 font-bold mr-2">
                    {processedResults.filter((r) => r.isDuplicate).length} duplicado(s)
                  </span>
                )}
                Cola activa
              </span>
            </div>

            {/* Lista Horizontal / Carrusel de Tickets del Lote */}
            <div className="flex gap-2 overflow-x-auto pb-2 scrollbar-thin">
              {processedResults.map((item, idx) => {
                const isSelected = item.ticketId === activeTicketId;
                return (
                  <button
                    key={item.ticketId}
                    type="button"
                    onClick={() => setActiveTicketId(item.ticketId)}
                    className={cn(
                      "flex items-center gap-2 rounded-lg border px-3 py-2 text-left transition-all shrink-0 text-xs",
                      isSelected
                        ? "border-brand bg-brand-soft text-brand font-bold shadow-sm"
                        : "border-line bg-panel-2 text-ink hover:border-line-2"
                    )}
                  >
                    <span className="mono text-[10px] text-muted">#{idx + 1}</span>
                    <div className="flex flex-col max-w-[120px]">
                      <span className="truncate text-xs font-semibold">{item.name}</span>
                      <span className="text-[10px] font-normal opacity-80">
                        {item.isDuplicate ? "⚠️ Duplicado" : item.estado}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Visor de Bitácora SSE en Tiempo Real para el Ticket Seleccionado */}
          {activeTicketId && (
            <div className="rounded-xl border border-line bg-panel p-4 shadow-sm">
              <div className="mb-2 flex items-center justify-between border-b border-line pb-2">
                <div>
                  <b className="text-xs font-bold text-ink">
                    Bitácora en Vivo del Agente
                  </b>
                  <p className="text-[11px] text-muted">
                    Ticket ID: <span className="mono">{activeTicketId.slice(0, 8)}</span>
                  </p>
                </div>

                {processedResults.find((r) => r.ticketId === activeTicketId)?.isDuplicate && (
                  <span className="rounded-md bg-amber-500/10 border border-amber-500/30 px-2 py-0.5 text-[10px] font-bold text-amber-600 dark:text-amber-400">
                    Comprobante Duplicado
                  </span>
                )}
              </div>

              {processedResults.find((r) => r.ticketId === activeTicketId)?.isDuplicate ? (
                <div className="p-4 text-center rounded-xl bg-panel-2 border border-line my-2">
                  <Copy className="h-8 w-8 text-amber-500 mx-auto mb-2 opacity-80" />
                  <b className="text-xs font-bold text-ink">Ticket descartado por duplicidad</b>
                  <p className="text-xs text-muted mt-1 max-w-sm mx-auto">
                    {processedResults.find((r) => r.ticketId === activeTicketId)?.errorMsg ||
                      "Este comprobante ya existe en el sistema o fue facturado previamente."}
                  </p>
                </div>
              ) : (
                <TicketStreamLog
                  ticketId={activeTicketId}
                  tenantId={tenantId}
                  onFinished={onSuccess}
                />
              )}
            </div>
          )}

          {/* Acciones Finales */}
          <div className="flex gap-2 justify-end pt-1">
            <button
              type="button"
              onClick={handleReset}
              className="rounded-xl border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink hover:bg-panel-2 transition-colors"
            >
              Subir otro lote
            </button>
            {onClose && (
              <button
                type="button"
                onClick={onClose}
                className="rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand hover:brightness-105 transition-colors shadow-sm"
              >
                Ver todos en la tabla
              </button>
            )}
          </div>
        </div>
      )}

      {errorMsg && (
        <div className="mt-3 flex items-center gap-2 rounded-xl border border-bad/30 bg-bad-soft p-3 text-xs font-medium text-bad">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{errorMsg}</span>
        </div>
      )}
    </div>
  );
}
