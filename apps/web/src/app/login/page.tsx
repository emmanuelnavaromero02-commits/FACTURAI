"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { loginWithGoogle } from "@/lib/api";
import { GoogleSignIn } from "@/components/GoogleSignIn";
import { Check, ShieldCheck, AlertCircle, Sparkles, X, User, Key, ExternalLink, ChevronDown, ChevronUp } from "lucide-react";
import { isRealGoogleClientId } from "@/components/GoogleSignIn";

export default function LoginPage() {
  const { loginDevMode, refreshUser, loading } = useAuth();
  const router = useRouter();

  const [devEmail, setDevEmail] = useState("emmanuelnavaromero02@gmail.com");
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [infoMessage, setInfoMessage] = useState<string | null>(null);

  // Client ID configurable dinámicamente desde el navegador
  const [customClientId, setCustomClientId] = useState<string>(() => {
    if (typeof window !== "undefined") {
      return (
        localStorage.getItem("facturai_google_client_id") ||
        process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ||
        ""
      );
    }
    return "";
  });
  const [inputClientId, setInputClientId] = useState(customClientId);
  const [showConfig, setShowConfig] = useState(false);
  const [configSaved, setConfigSaved] = useState(false);

  const isDev =
    process.env.NEXT_PUBLIC_ENV === "development" ||
    typeof window !== "undefined" &&
      (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");

  const hasConfiguredRealId = isRealGoogleClientId(customClientId);

  const handleGoogleSuccess = async (idToken: string) => {
    setSubmitting(true);
    setErrorMessage(null);
    setInfoMessage(null);
    try {
      await loginWithGoogle(idToken);
      await refreshUser();
      router.push("/app");
    } catch (err: unknown) {
      console.error("Error al autenticar con Google:", err);
      const msg =
        (err as { message?: string })?.message ||
        "Error al validar credenciales con Google OAuth.";
      setErrorMessage(msg);
    } finally {
      setSubmitting(false);
    }
  };

  const handleGoogleError = (msg: string) => {
    setErrorMessage(msg);
  };

  const handleFallbackClick = () => {
    setInfoMessage(
      "Google requiere que registres tu aplicación en Google Cloud Console para darte un Client ID. Si aún no lo has configurado, puedes pegarlo abajo en 'Configurar Google Client ID' o entrar de inmediato con 1 clic usando el panel de abajo."
    );
    setShowConfig(true);
  };

  const handleSaveClientId = (e: React.FormEvent) => {
    e.preventDefault();
    const clean = inputClientId.trim();
    if (!clean) return;
    if (typeof window !== "undefined") {
      localStorage.setItem("facturai_google_client_id", clean);
    }
    setCustomClientId(clean);
    setConfigSaved(true);
    setInfoMessage(null);
    setTimeout(() => setConfigSaved(false), 3000);
  };

  const handleQuickLogin = async (email: string) => {
    setSubmitting(true);
    setErrorMessage(null);
    setInfoMessage(null);
    try {
      await loginDevMode(email);
      router.push("/app");
    } catch (err: unknown) {
      console.error(err);
      setErrorMessage(
        "Error al conectar con FacturAI API. Verifica que el backend esté activo en el puerto 8000."
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleDevSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!devEmail.trim()) return;
    await handleQuickLogin(devEmail.trim());
  };

  return (
    <div className="min-h-screen bg-bg text-ink lg:grid lg:grid-cols-[1.05fr_0.95fr]">
      {/* Columna Izquierda: Formulario de Autenticación */}
      <div className="mx-auto flex w-full max-w-[440px] flex-col justify-center px-6 py-12 lg:px-10">
        <div className="mb-8 flex items-center gap-2.5 text-lg font-bold tracking-tight text-ink">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand text-sm font-black text-on-brand shadow-sm">
            F
          </span>
          <span>
            Factur<span className="text-brand">AI</span>
          </span>
        </div>

        <div className="mb-6">
          <h1 className="text-3xl font-extrabold leading-tight tracking-tight text-ink">
            Fotografía el ticket. La factura llega a tu correo.
          </h1>
          <p className="mt-3 text-[14.5px] leading-relaxed text-muted">
            Un agente autónomo lee el ticket, entra al portal del comercio y factura por ti.
            Si el portal pide captcha o verificación, resuelve automáticamente o te asiste en 5 segundos.
          </p>
        </div>

        {/* Mensaje de Error */}
        {errorMessage && (
          <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-bad/30 bg-bad-soft p-3 text-xs text-bad">
            <AlertCircle className="h-4 w-4 shrink-0 stroke-[2.5]" />
            <div className="flex-1 leading-relaxed">{errorMessage}</div>
            <button
              onClick={() => setErrorMessage(null)}
              className="text-bad/70 hover:text-bad"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {/* Mensaje Informativo / Guía */}
        {infoMessage && (
          <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-brand/30 bg-brand-soft p-3 text-xs text-ink leading-relaxed">
            <Sparkles className="h-4 w-4 shrink-0 text-brand mt-0.5" />
            <div className="flex-1">{infoMessage}</div>
            <button
              onClick={() => setInfoMessage(null)}
              className="text-muted hover:text-ink"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {/* Bloque Principal de Autenticación Google */}
        <div className="flex flex-col gap-3">
          <GoogleSignIn
            onSuccess={handleGoogleSuccess}
            onError={handleGoogleError}
            onFallbackClick={handleFallbackClick}
            customClientId={customClientId}
            loading={submitting || loading}
          />

          {/* Configuración de Client ID de Google */}
          <div className="rounded-xl border border-line bg-panel p-3">
            <button
              type="button"
              onClick={() => setShowConfig(!showConfig)}
              className="flex w-full items-center justify-between text-left text-xs font-medium text-muted hover:text-ink"
            >
              <span className="flex items-center gap-1.5">
                <Key className="h-3.5 w-3.5 text-brand" />
                <span>
                  {hasConfiguredRealId
                    ? "✓ Google Client ID activo"
                    : "Configurar Client ID de Google Cloud"}
                </span>
              </span>
              {showConfig ? (
                <ChevronUp className="h-3.5 w-3.5" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" />
              )}
            </button>

            {showConfig && (
              <div className="mt-3 border-t border-line pt-3 text-xs text-muted">
                <p className="mb-2 leading-relaxed">
                  Para que Google abra su ventana oficial, genera un <b>ID de cliente de OAuth</b> tipo <i>Web Application</i> en{" "}
                  <a
                    href="https://console.cloud.google.com/apis/credentials"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 font-semibold text-brand underline"
                  >
                    Google Cloud Console <ExternalLink className="h-3 w-3 inline" />
                  </a>{" "}
                  con origen <code>http://localhost:3000</code>:
                </p>
                <form onSubmit={handleSaveClientId} className="flex gap-2">
                  <input
                    type="text"
                    value={inputClientId}
                    onChange={(e) => setInputClientId(e.target.value)}
                    placeholder="...apps.googleusercontent.com"
                    className="flex-1 rounded-lg border border-line-2 bg-panel-2 px-2.5 py-1.5 font-mono text-[11px] text-ink outline-none focus:border-brand"
                  />
                  <button
                    type="submit"
                    className="rounded-lg bg-brand px-3 py-1.5 font-semibold text-on-brand hover:brightness-105"
                  >
                    Guardar
                  </button>
                </form>
                {configSaved && (
                  <p className="mt-1.5 text-[11px] font-medium text-ok">
                    ✓ Client ID guardado. El botón oficial de Google está listo.
                  </p>
                )}
              </div>
            )}
          </div>

          {/* Opciones Rápidas en Desarrollo / Local */}
          {isDev && (
            <div className="mt-1 rounded-2xl border border-line bg-panel p-4 shadow-sm">
              <div className="mb-2.5 flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-muted">
                  <ShieldCheck className="h-3.5 w-3.5 text-brand" />
                  Acceso Rápido FacturAI
                </span>
                <span className="rounded-md bg-brand-soft px-2 py-0.5 text-[10px] font-semibold text-brand">
                  1-Clic
                </span>
              </div>

              <p className="mb-3 text-[11.5px] leading-relaxed text-muted">
                Prueba FacturAI de inmediato sin esperar a Google Cloud Console:
              </p>

              {/* Botones de Cuentas Preconfiguradas */}
              <div className="mb-3 flex flex-wrap gap-1.5">
                <button
                  type="button"
                  onClick={() => handleQuickLogin("emmanuelnavaromero02@gmail.com")}
                  disabled={submitting}
                  className="flex items-center gap-1.5 rounded-lg border border-line-2 bg-panel-2 px-2.5 py-1.5 text-xs font-medium text-ink transition-colors hover:border-brand hover:text-brand"
                >
                  <User className="h-3 w-3 text-muted" />
                  <span>emmanuelnavaromero02@gmail.com</span>
                </button>
                <button
                  type="button"
                  onClick={() => handleQuickLogin("emmanuel@navamero.mx")}
                  disabled={submitting}
                  className="flex items-center gap-1.5 rounded-lg border border-line-2 bg-panel-2 px-2.5 py-1.5 text-xs font-medium text-ink transition-colors hover:border-brand hover:text-brand"
                >
                  <User className="h-3 w-3 text-muted" />
                  <span>emmanuel@navamero.mx</span>
                </button>
              </div>

              {/* Formulario de Correo Personalizado */}
              <form onSubmit={handleDevSubmit} className="flex gap-2">
                <input
                  type="email"
                  value={devEmail}
                  onChange={(e) => setDevEmail(e.target.value)}
                  placeholder="otro.correo@gmail.com"
                  className="flex-1 rounded-lg border border-line-2 bg-panel-2 px-3 py-1.5 text-xs text-ink outline-none focus:border-brand"
                  required
                />
                <button
                  type="submit"
                  disabled={submitting || loading}
                  className="rounded-lg bg-brand px-3 py-1.5 text-xs font-bold text-on-brand shadow transition-all hover:brightness-105 active:scale-[0.98] disabled:opacity-50"
                >
                  {submitting ? "Entrando..." : "Entrar"}
                </button>
              </form>
            </div>
          )}
        </div>

        <div className="my-6 flex items-center gap-3 text-xs text-muted">
          <div className="h-px flex-1 bg-line" />
          <span>tu correo de Google recibe las facturas</span>
          <div className="h-px flex-1 bg-line" />
        </div>

        {/* Bullets de Privacidad */}
        <ul className="flex flex-col gap-2.5 text-xs text-ink-2">
          <li className="flex items-start gap-2.5">
            <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-ok-soft text-ok">
              <Check className="h-2.5 w-2.5 stroke-[3]" />
            </span>
            <span>Cada cuenta ve únicamente sus tickets, datos fiscales y RFCs.</span>
          </li>
          <li className="flex items-start gap-2.5">
            <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-ok-soft text-ok">
              <Check className="h-2.5 w-2.5 stroke-[3]" />
            </span>
            <span>La imagen del ticket se borra en cuanto se factura exitosamente.</span>
          </li>
          <li className="flex items-start gap-2.5">
            <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-ok-soft text-ok">
              <Check className="h-2.5 w-2.5 stroke-[3]" />
            </span>
            <span>El CFDI no se guarda en disco: lo manda el emisor a tu correo.</span>
          </li>
        </ul>
      </div>

      {/* Columna Derecha: Tarjetas del Flujo (Desktop) */}
      <div className="hidden flex-col justify-center gap-3.5 border-l border-line bg-panel-2 p-12 lg:flex">
        <p className="lt mb-1">El recorrido autónomo en FacturAI</p>

        <div className="flex items-start gap-3 rounded-xl border border-line bg-panel p-4 shadow-prototipo">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-xs font-bold text-brand">
            1
          </span>
          <div>
            <b className="block text-[13.5px] font-semibold text-ink">Foto del Ticket</b>
            <span className="text-[12.5px] text-muted">
              Desde tu celular o computadora, en el mismo instante de la compra.
            </span>
          </div>
        </div>

        <div className="flex items-start gap-3 rounded-xl border border-line bg-panel p-4 shadow-prototipo">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-xs font-bold text-brand">
            2
          </span>
          <div>
            <b className="block text-[13.5px] font-semibold text-ink">Lectura con Visión</b>
            <span className="text-[12.5px] text-muted">
              Extracción instantánea de folio, total, fecha, emisor y portal de facturación.
            </span>
          </div>
        </div>

        <div className="flex items-start gap-3 rounded-xl border border-line bg-panel p-4 shadow-prototipo">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-xs font-bold text-brand">
            3
          </span>
          <div>
            <b className="block text-[13.5px] font-semibold text-ink">Motor Autónomo y Permutaciones</b>
            <span className="text-[12.5px] text-muted">
              Si el portal rechaza un formato, el agente prueba en silencio variaciones hasta timbrar.
            </span>
          </div>
        </div>

        <div className="flex items-start gap-3 rounded-xl border border-line bg-panel p-4 shadow-prototipo">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-xs font-bold text-brand">
            4
          </span>
          <div>
            <b className="block text-[13.5px] font-semibold text-ink">Resolución Humana de Captcha</b>
            <span className="text-[12.5px] text-muted">
              Resuelve Cloudflare y reCAPTCHA con curvas orgánicas; solo pide ayuda si es estrictamente necesario.
            </span>
          </div>
        </div>

        <div className="flex items-start gap-3 rounded-xl border border-line bg-panel p-4 shadow-prototipo">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-xs font-bold text-brand">
            5
          </span>
          <div>
            <b className="block text-[13.5px] font-semibold text-ink">Factura al Correo y Cero Rastro</b>
            <span className="text-[12.5px] text-muted">
              XML y PDF directos a tu bandeja, imagen eliminada y privacidad total.
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

