"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";

export default function RootPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading) {
      if (user) {
        router.replace("/app");
      } else {
        router.replace("/login");
      }
    }
  }, [user, loading, router]);

  // Fallback de seguridad: si tras 1.2 segundos sigue en la raíz, redirigir de inmediato
  useEffect(() => {
    const timer = setTimeout(() => {
      if (user) {
        router.replace("/app");
      } else {
        router.replace("/login");
      }
    }, 1200);
    return () => clearTimeout(timer);
  }, [user, router]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-bg px-4">
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="h-9 w-9 animate-spin rounded-full border-2 border-brand border-t-transparent" />
        <span className="text-xs font-semibold uppercase tracking-widest text-muted">
          Cargando FacturAI...
        </span>
        <div className="mt-2 flex gap-3 text-xs text-muted">
          <a href="/login" className="underline hover:text-ink">
            Iniciar Sesión
          </a>
          <span>•</span>
          <a href="/app" className="underline hover:text-ink">
            Ir al Panel
          </a>
        </div>
      </div>
    </div>
  );
}
