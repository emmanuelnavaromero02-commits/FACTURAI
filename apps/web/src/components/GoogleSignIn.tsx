"use client";

import React, { useEffect, useRef, useState } from "react";
import Script from "next/script";

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string;
            callback: (response: { credential?: string }) => void;
            auto_select?: boolean;
            cancel_on_tap_outside?: boolean;
          }) => void;
          renderButton: (
            parent: HTMLElement,
            options: {
              theme?: "outline" | "filled_blue" | "filled_black";
              size?: "large" | "medium" | "small";
              type?: "standard" | "icon";
              text?: "signin_with" | "signup_with" | "continue_with";
              shape?: "rectangular" | "pill" | "circle";
              logo_alignment?: "left" | "center";
              width?: number | string;
            }
          ) => void;
          prompt: (momentListener?: (notification: unknown) => void) => void;
        };
      };
    };
  }
}

interface GoogleSignInProps {
  onSuccess: (idToken: string) => Promise<void>;
  onError: (errMsg: string) => void;
  onFallbackClick?: () => void;
  loading?: boolean;
  customClientId?: string;
}

export function isRealGoogleClientId(id?: string): boolean {
  if (!id) return false;
  const clean = id.trim();
  if (
    clean.includes("local") ||
    clean.includes("placeholder") ||
    clean.includes("example") ||
    clean.includes("facturia") ||
    clean.includes("facturai")
  ) {
    return false;
  }
  // Los Client IDs de Google Cloud válidos comienzan con números (Project ID)
  return /^[0-9]+-[a-z0-9_]+\.apps\.googleusercontent\.com$/i.test(clean);
}

export function GoogleSignIn({
  onSuccess,
  onError,
  onFallbackClick,
  loading = false,
  customClientId,
}: GoogleSignInProps) {
  const [gsiLoaded, setGsiLoaded] = useState(false);
  const [buttonRendered, setButtonRendered] = useState(false);
  const googleBtnContainerRef = useRef<HTMLDivElement>(null);

  const clientId =
    customClientId ||
    (typeof window !== "undefined" ? localStorage.getItem("facturai_google_client_id") : null) ||
    process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ||
    "facturia-local-client-id.apps.googleusercontent.com";

  const hasRealClientId = isRealGoogleClientId(clientId);

  // Verificar si ya estaba cargado el script previamente
  useEffect(() => {
    if (typeof window !== "undefined" && window.google?.accounts?.id) {
      setGsiLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (
      !hasRealClientId ||
      !gsiLoaded ||
      typeof window === "undefined" ||
      !window.google?.accounts?.id
    ) {
      setButtonRendered(false);
      return;
    }

    try {
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: async (res) => {
          if (res.credential) {
            try {
              await onSuccess(res.credential);
            } catch (err: unknown) {
              const msg =
                (err as { message?: string })?.message ||
                "Error al validar el token de Google con FacturAI.";
              onError(msg);
            }
          } else {
            onError("No se recibió credencial válida de Google.");
          }
        },
        auto_select: false,
        cancel_on_tap_outside: true,
      });

      if (googleBtnContainerRef.current) {
        googleBtnContainerRef.current.innerHTML = "";
        window.google.accounts.id.renderButton(googleBtnContainerRef.current, {
          theme: "outline",
          size: "large",
          type: "standard",
          text: "continue_with",
          shape: "rectangular",
          logo_alignment: "left",
          width: 380,
        });
        setButtonRendered(true);
      }
    } catch (e) {
      console.warn("Google Identity Services no pudo renderizar el iframe:", e);
      setButtonRendered(false);
    }
  }, [gsiLoaded, clientId, onSuccess, onError]);

  return (
    <div className="w-full">
      <Script
        src="https://accounts.google.com/gsi/client"
        strategy="afterInteractive"
        onLoad={() => setGsiLoaded(true)}
      />

      {/* Contenedor del Botón Oficial de Google SDK */}
      <div
        ref={googleBtnContainerRef}
        className={`flex w-full justify-center ${
          buttonRendered ? "min-h-[44px]" : "hidden"
        }`}
      />

      {/* Botón Alternativo / Fallback con estilo oficial Google */}
      {(!buttonRendered || loading) && (
        <button
          type="button"
          disabled={loading}
          onClick={() => {
            if (onFallbackClick) {
              onFallbackClick();
            } else if (window.google?.accounts?.id) {
              try {
                window.google.accounts.id.prompt();
              } catch {
                onError("No se pudo iniciar el flujo de Google. Revisa tu conexión.");
              }
            }
          }}
          className="flex w-full items-center justify-center gap-3 rounded-xl border border-line-2 bg-panel py-3.5 text-[15px] font-semibold text-ink shadow-sm transition-all hover:border-brand hover:bg-panel-2 active:scale-[0.99] disabled:opacity-50"
        >
          <svg className="h-4.5 w-4.5" viewBox="0 0 24 24">
            <path
              fill="#4285F4"
              d="M23 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.2a5.3 5.3 0 0 1-2.3 3.5v2.9h3.7c2.2-2 3.4-5 3.4-8.6z"
            />
            <path
              fill="#34A853"
              d="M12 23.5c3.1 0 5.7-1 7.6-2.8l-3.7-2.9c-1 .7-2.3 1.1-3.9 1.1-3 0-5.6-2-6.5-4.8H1.7v3C3.6 21 7.5 23.5 12 23.5z"
            />
            <path
              fill="#FBBC05"
              d="M5.5 14.1a6.9 6.9 0 0 1 0-4.4v-3H1.7a11.5 11.5 0 0 0 0 10.4l3.8-3z"
            />
            <path
              fill="#EA4335"
              d="M12 5.4c1.7 0 3.2.6 4.4 1.7l3.3-3.3C17.7 1.9 15.1.8 12 .8 7.5.8 3.6 3.4 1.7 7.1l3.8 3c.9-2.8 3.5-4.7 6.5-4.7z"
            />
          </svg>
          <span>{loading ? "Verificando con Google..." : "Continuar con Google"}</span>
        </button>
      )}
    </div>
  );
}
