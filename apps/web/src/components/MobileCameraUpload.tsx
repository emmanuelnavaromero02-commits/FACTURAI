"use client";

import React, { useRef, useState } from "react";
import { Camera, Upload, X, AlertTriangle, ArrowRight } from "lucide-react";
import { uploadTicket } from "@/lib/api";
import { TicketStreamLog } from "./TicketStreamLog";
import { cn } from "@/lib/utils";

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
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [ticketId, setTicketId] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Advertencia de fecha de mes anterior
  const [showMonthWarning, setShowMonthWarning] = useState(false);
  const [confirmedMonthWarning, setConfirmedMonthWarning] = useState(false);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // Validación de tamaño (máx 10 MB)
    if (file.size > 10 * 1024 * 1024) {
      setErrorMsg("El archivo excede el tamaño máximo permitido de 10 MB.");
      return;
    }

    setErrorMsg(null);
    setSelectedFile(file);

    // Generar preview para imágenes (en HEIC en browsers compatibles o JPEG/PNG)
    if (file.type.startsWith("image/") || file.name.match(/\.(jpe?g|png|webp|heic|heif)$/i)) {
      const url = URL.createObjectURL(file);
      setPreviewUrl(url);
    } else {
      setPreviewUrl(null);
    }
  };

  const handleStartProcessing = async () => {
    if (!selectedFile) return;

    setUploading(true);
    setErrorMsg(null);

    try {
      const resp = await uploadTicket(tenantId, selectedFile);
      setTicketId(resp.id);
    } catch (err: unknown) {
      setErrorMsg(
        (err as Error).message || "Ocurrió un error al subir el ticket al servidor."
      );
      setUploading(false);
    }
  };

  const handleReset = () => {
    setSelectedFile(null);
    setPreviewUrl(null);
    setUploading(false);
    setTicketId(null);
    setErrorMsg(null);
    setShowMonthWarning(false);
    setConfirmedMonthWarning(false);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  return (
    <div className="w-full">
      {/* Input oculto con capture="environment" para abrir la cámara trasera en iPhone/Android */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*,.heic,.heif,application/pdf"
        capture="environment"
        onChange={handleFileChange}
        className="hidden"
      />

      {/* Vista 1: Selector o Botón Gigante para Móvil */}
      {!selectedFile && !ticketId && (
        <div
          onClick={() => fileInputRef.current?.click()}
          className="group flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed border-line-2 bg-panel-2 p-8 text-center transition-all hover:border-brand hover:bg-brand-soft active:scale-[0.99]"
        >
          <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-brand text-on-brand shadow-lg transition-transform group-hover:scale-105">
            <Camera className="h-8 w-8 stroke-[2.2]" />
          </div>

          <b className="text-lg font-bold text-ink">
            Fotografiar ticket o elegir imagen
          </b>
          <span className="mt-1 text-xs text-muted">
            JPG, PNG, HEIC (iPhone) o PDF · Hasta 10 MB
          </span>

          <div className="mt-5 flex items-center gap-2 rounded-full border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink-2 shadow-sm">
            <Upload className="h-3.5 w-3.5 text-brand" />
            <span>Toca aquí con una sola mano</span>
          </div>
        </div>
      )}

      {/* Vista 2: Preview y Confirmación antes de gastar en el Agente */}
      {selectedFile && !ticketId && (
        <div className="flex flex-col gap-4">
          <div className="relative overflow-hidden rounded-xl border border-line bg-panel-2">
            {previewUrl ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={previewUrl}
                alt="Vista previa del ticket"
                className="max-h-72 w-full object-contain bg-black/5"
              />
            ) : (
              <div className="flex h-44 items-center justify-center text-sm font-medium text-muted">
                Archivo seleccionado: {selectedFile.name}
              </div>
            )}

            <button
              onClick={handleReset}
              className="absolute right-3 top-3 flex h-8 w-8 items-center justify-center rounded-full bg-black/60 text-white backdrop-blur-sm transition-opacity hover:bg-black/80"
              aria-label="Quitar archivo"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="flex items-center justify-between text-xs text-muted">
            <span className="truncate max-w-[200px]">{selectedFile.name}</span>
            <span>{Math.round(selectedFile.size / 1024)} KB</span>
          </div>

          {/* Advertencia de mes anterior (si aplica) */}
          {showMonthWarning && !confirmedMonthWarning && (
            <div className="flex flex-col gap-2 rounded-xl border border-warn/40 bg-warn-soft p-3.5 text-xs text-ink-2">
              <div className="flex items-center gap-2 font-bold text-warn">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>Ticket de mes anterior</span>
              </div>
              <p>
                Muchos portales de comercio solo permiten facturar compras del mes en curso. Si continúas, el agente intentará facturarlo de todos modos.
              </p>
              <div className="mt-1 flex gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmedMonthWarning(true)}
                  className="rounded-lg bg-warn px-3 py-1.5 font-semibold text-warn-soft hover:brightness-105"
                >
                  Continuar de todos modos
                </button>
                <button
                  type="button"
                  onClick={handleReset}
                  className="rounded-lg border border-line-2 bg-panel px-3 py-1.5 font-medium text-ink hover:bg-panel-2"
                >
                  Cancelar
                </button>
              </div>
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleReset}
              className="flex-1 rounded-xl border border-line-2 bg-panel py-3 text-sm font-semibold text-ink transition-colors hover:bg-panel-2"
            >
              Tomar otra
            </button>
            <button
              type="button"
              disabled={uploading}
              onClick={handleStartProcessing}
              className={cn(
                "flex flex-1 items-center justify-center gap-2 rounded-xl bg-brand py-3 text-sm font-bold text-on-brand shadow-md transition-all hover:brightness-110 active:scale-[0.98]",
                uploading && "opacity-70"
              )}
            >
              <span>{uploading ? "Subiendo..." : "Procesar ticket"}</span>
              <ArrowRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}

      {/* Vista 3: Ticket Procesándose en Vivo con Stream SSE */}
      {ticketId && (
        <div className="flex flex-col gap-4">
          <div className="rounded-xl border border-line bg-panel p-4 shadow-sm">
            <div className="mb-2 flex items-center justify-between border-b border-line pb-2.5">
              <div>
                <b className="text-sm font-bold text-ink">Bitácora del Agente</b>
                <p className="text-xs text-muted">Procesamiento en tiempo real</p>
              </div>
              <span className="mono text-xs text-muted">ID: {ticketId.slice(0, 8)}</span>
            </div>

            <TicketStreamLog
              ticketId={ticketId}
              tenantId={tenantId}
              onFinished={onSuccess}
            />
          </div>

          <div className="flex gap-2 justify-end">
            <button
              type="button"
              onClick={handleReset}
              className="rounded-xl border border-line-2 bg-panel px-4 py-2 text-xs font-semibold text-ink hover:bg-panel-2"
            >
              Subir otro ticket
            </button>
            {onClose && (
              <button
                type="button"
                onClick={onClose}
                className="rounded-xl bg-brand px-4 py-2 text-xs font-semibold text-on-brand hover:brightness-105"
              >
                Ver en la tabla
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
