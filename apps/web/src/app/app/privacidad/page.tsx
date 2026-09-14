"use client";

import React, { useState } from "react";
import { Check } from "lucide-react";

export default function PrivacidadPage() {
  const [toastMsg, setToastMsg] = useState<string | null>(null);

  const showToast = (msg: string) => {
    setToastMsg(msg);
    setTimeout(() => setToastMsg(null), 2500);
  };

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-ink">
          Privacidad y retención
        </h1>
        <p className="mt-0.5 text-xs text-muted">
          Qué se guarda, por cuánto tiempo y qué nunca toca el servidor de forma permanente.
        </p>
      </div>

      {/* Tarjeta de Políticas */}
      <div className="overflow-hidden rounded-2xl border border-line bg-panel shadow-prototipo">
        <div className="divide-y divide-line">
          {/* Fila 1: Imagen del Ticket */}
          <div className="flex items-center justify-between gap-4 p-4.5">
            <div className="flex items-center gap-3.5 min-w-0">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-line bg-panel-2 text-base">
                🖼️
              </span>
              <div className="min-w-0">
                <b className="block text-sm font-semibold text-ink">
                  Imagen del ticket
                </b>
                <span className="block text-xs text-muted">
                  Se elimina en cuanto el CFDI se confirma o tras 24h. Nunca se respalda.
                </span>
              </div>
            </div>
            <span className="rounded-md border border-ok/40 bg-ok-soft px-2.5 py-0.5 text-[10.5px] font-semibold text-ok uppercase">
              Activo
            </span>
          </div>

          {/* Fila 2: Archivo del CFDI */}
          <div className="flex items-center justify-between gap-4 p-4.5">
            <div className="flex items-center gap-3.5 min-w-0">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-line bg-panel-2 text-base">
                📄
              </span>
              <div className="min-w-0">
                <b className="block text-sm font-semibold text-ink">
                  Archivo del CFDI (PDF y XML)
                </b>
                <span className="block text-xs text-muted">
                  No se almacena en base de datos: el emisor lo manda a tu correo, o descarga efímera en RAM de 30 min.
                </span>
              </div>
            </div>
            <span className="rounded-md border border-ok/40 bg-ok-soft px-2.5 py-0.5 text-[10.5px] font-semibold text-ok uppercase">
              Efímero
            </span>
          </div>

          {/* Fila 3: Metadatos del Ticket */}
          <div className="flex items-center justify-between gap-4 p-4.5">
            <div className="flex items-center gap-3.5 min-w-0">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-line bg-panel-2 text-base">
                📋
              </span>
              <div className="min-w-0">
                <b className="block text-sm font-semibold text-ink">
                  Metadatos del ticket
                </b>
                <span className="block text-xs text-muted">
                  Comercio, folio, monto y fecha — para historial, deducción y evitar facturas duplicadas.
                </span>
              </div>
            </div>
            <span className="rounded-md border border-line-2 bg-panel-2 px-2.5 py-0.5 text-[10.5px] font-semibold text-muted uppercase">
              90 días
            </span>
          </div>

          {/* Fila 4: Credenciales de Portales */}
          <div className="flex items-center justify-between gap-4 p-4.5">
            <div className="flex items-center gap-3.5 min-w-0">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-line bg-panel-2 text-base">
                🔑
              </span>
              <div className="min-w-0">
                <b className="block text-sm font-semibold text-ink">
                  Credenciales de portales
                </b>
                <span className="block text-xs text-muted">
                  Cifradas por tenant con AES-256-GCM; se descifran en memoria solo dentro de la sesión que factura.
                </span>
              </div>
            </div>
            <span className="rounded-md border border-live/40 bg-live-soft px-2.5 py-0.5 text-[10.5px] font-semibold text-live uppercase">
              Cifrado
            </span>
          </div>

          {/* Fila 5: Sesión del Navegador */}
          <div className="flex items-center justify-between gap-4 p-4.5">
            <div className="flex items-center gap-3.5 min-w-0">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-line bg-panel-2 text-base">
                🎥
              </span>
              <div className="min-w-0">
                <b className="block text-sm font-semibold text-ink">
                  Sesión del navegador
                </b>
                <span className="block text-xs text-muted">
                  El navegador corre aislado en el worker; las capturas viajan solo mientras el handoff está activo. No se graba video.
                </span>
              </div>
            </div>
            <span className="rounded-md border border-line-2 bg-panel-2 px-2.5 py-0.5 text-[10.5px] font-semibold text-muted uppercase">
              Efímero
            </span>
          </div>
        </div>
      </div>

      {/* Nota de Destrucción de Datos (docs/prototipo.html:366) */}
      <div className="rounded-xl border border-line bg-panel-2 p-4 text-xs leading-relaxed text-ink-2">
        Si borras un ticket con el botón de eliminar, en el mismo segundo se borra su registro en la base de datos y la imagen en S3/MinIO. Lo único que sobrevive es tu propio CFDI, que ya está en tu buzón de correo y no en este sistema.
      </div>

      {toastMsg && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-2 rounded-full bg-ink px-4 py-2 text-xs font-semibold text-bg shadow-xl animate-slide-in">
          <Check className="h-4 w-4 text-ok" />
          <span>{toastMsg}</span>
        </div>
      )}
    </div>
  );
}
