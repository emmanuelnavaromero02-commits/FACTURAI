"use client";

import React, { useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import {
  Receipt,
  FileText,
  Users,
  Shield,
  ChevronDown,
  LogOut,
  Plus,
  Building2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { createTenant } from "@/lib/api";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { user, loading, activeTenant, setActiveTenantId, logoutUser, refreshUser } =
    useAuth();
  const pathname = usePathname();
  const router = useRouter();

  const [tenantDropdownOpen, setTenantDropdownOpen] = useState(false);
  const [showNewTenantModal, setShowNewTenantModal] = useState(false);
  const [newTenantName, setNewTenantName] = useState("");
  const [creatingTenant, setCreatingTenant] = useState(false);

  // Redirigir a login si no hay sesión
  React.useEffect(() => {
    if (!loading && !user) {
      router.replace("/login");
    }
  }, [user, loading, router]);

  if (loading || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-brand border-t-transparent" />
          <span className="text-xs font-semibold uppercase tracking-widest text-muted">
            Cargando sesión...
          </span>
        </div>
      </div>
    );
  }

  const handleCreateTenantSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTenantName.trim()) return;
    setCreatingTenant(true);
    try {
      const created = await createTenant(newTenantName.trim());
      await refreshUser();
      setActiveTenantId(created.id);
      setShowNewTenantModal(false);
      setNewTenantName("");
      setTenantDropdownOpen(false);
    } catch (err: unknown) {
      alert((err as Error).message || "Error al crear empresa.");
    } finally {
      setCreatingTenant(false);
    }
  };

  const navItems = [
    { href: "/app", label: "Tickets", icon: Receipt },
    { href: "/app/datos", label: "Datos fiscales", icon: FileText },
    { href: "/app/equipo", label: "Equipo", icon: Users },
    { href: "/app/privacidad", label: "Privacidad", icon: Shield },
  ];

  const tenantInitials = (activeTenant?.nombre || "Empresa")
    .split(" ")
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();

  const userInitials = (user.nombre || user.email)
    .split(" ")
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();

  return (
    <div className="min-h-screen bg-bg text-ink lg:grid lg:grid-cols-[240px_1fr]">
      {/* BARRA LATERAL (Desktop) & BARRA SUPERIOR (Mobile) */}
      <aside className="sticky top-0 z-30 flex flex-col border-b border-line bg-panel p-3 lg:h-screen lg:border-b-0 lg:border-r lg:p-4">
        {/* Marca Oficial FacturAI */}
        <div className="mb-3 hidden items-center gap-2.5 px-1 lg:flex">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand text-xs font-black text-on-brand shadow-sm">
            F
          </span>
          <span className="text-base font-bold tracking-tight text-ink">
            Factur<span className="text-brand">AI</span>
          </span>
        </div>

        {/* Selector de Empresa / Tenant */}
        <div className="relative mb-3 lg:mb-4">
          <button
            type="button"
            onClick={() => setTenantDropdownOpen(!tenantDropdownOpen)}
            className="flex w-full items-center gap-2.5 rounded-xl border border-line bg-panel-2 p-2.5 text-left transition-colors hover:border-line-2"
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand text-xs font-bold text-on-brand shadow-sm">
              {tenantInitials}
            </span>
            <div className="min-w-0 flex-1">
              <b className="block truncate text-[13px] font-semibold text-ink">
                {activeTenant?.nombre || "Mi Empresa"}
              </b>
              <span className="block truncate text-[11px] text-muted">
                {activeTenant?.plan?.toUpperCase() || "PLAN FREE"}
              </span>
            </div>
            {user.tenants.length > 1 && (
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
            )}
          </button>

          {/* Menú Desplegable de Empresas */}
          {tenantDropdownOpen && (
            <div className="absolute left-0 top-full z-50 mt-1.5 w-full rounded-xl border border-line bg-panel p-1.5 shadow-lg">
              <p className="lt px-2 py-1 text-[9px]">Tus empresas</p>
              <div className="flex flex-col gap-0.5">
                {user.tenants.map((t) => (
                  <button
                    key={t.id}
                    onClick={() => {
                      setActiveTenantId(t.id);
                      setTenantDropdownOpen(false);
                    }}
                    className={cn(
                      "flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs font-medium transition-colors hover:bg-panel-2",
                      t.id === activeTenant?.id && "bg-brand-soft font-semibold text-brand"
                    )}
                  >
                    <span className="truncate">{t.nombre}</span>
                    <span className="text-[10px] uppercase text-muted">{t.rol}</span>
                  </button>
                ))}

                <button
                  type="button"
                  onClick={() => {
                    setShowNewTenantModal(true);
                    setTenantDropdownOpen(false);
                  }}
                  className="mt-1 flex w-full items-center gap-1.5 border-t border-line px-2.5 pt-2 text-left text-xs font-semibold text-brand hover:underline"
                >
                  <Plus className="h-3 w-3" />
                  <span>Crear nueva empresa</span>
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Navegación */}
        <p className="lt hidden px-2.5 pb-2 text-[10px] lg:block">Operación</p>
        <nav className="flex flex-row gap-1 overflow-x-auto lg:flex-col lg:gap-1">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive =
              item.href === "/app"
                ? pathname === "/app" || pathname.startsWith("/app/tickets")
                : pathname.startsWith(item.href);

            return (
              <a
                key={item.href}
                href={item.href}
                className={cn(
                  "flex items-center gap-2.5 whitespace-nowrap rounded-lg px-3 py-2 text-[13.5px] font-medium transition-colors",
                  isActive
                    ? "bg-brand-soft font-semibold text-brand"
                    : "text-ink-2 hover:bg-panel-2 hover:text-ink"
                )}
              >
                <Icon className="h-4 w-4 shrink-0 stroke-[2]" />
                <span>{item.label}</span>
              </a>
            );
          })}
        </nav>

        {/* Pie de Barra: Usuario y Logout */}
        <div className="mt-auto hidden items-center justify-between border-t border-line pt-3 lg:flex">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-panel-3 text-[11px] font-bold text-ink-2">
              {userInitials}
            </span>
            <div className="min-w-0">
              <b className="block truncate text-[12px] font-semibold text-ink">
                {user.nombre}
              </b>
              <span className="block truncate text-[10.5px] text-muted">
                {activeTenant?.rol || "Miembro"}
              </span>
            </div>
          </div>

          <button
            onClick={() => logoutUser()}
            title="Cerrar sesión"
            className="flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-panel-2 hover:text-bad"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </aside>

      {/* CONTENIDO PRINCIPAL */}
      <main className="mx-auto w-full max-w-[1040px] px-4 py-6 pb-20 lg:px-7 lg:py-7">
        {children}
      </main>

      {/* Modal Crear Empresa */}
      {showNewTenantModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-xs">
          <div className="w-full max-w-sm rounded-2xl border border-line bg-panel p-5 shadow-xl">
            <div className="mb-4 flex items-center gap-2 font-bold text-ink">
              <Building2 className="h-5 w-5 text-brand" />
              <span>Nueva Empresa</span>
            </div>
            <form onSubmit={handleCreateTenantSubmit} className="flex flex-col gap-3">
              <label className="text-xs font-semibold text-muted">
                Nombre de la empresa o razón social:
                <input
                  type="text"
                  value={newTenantName}
                  onChange={(e) => setNewTenantName(e.target.value)}
                  placeholder="Ej: Acero Norte S.A."
                  className="mt-1 w-full rounded-lg border border-line-2 bg-panel-2 p-2.5 text-sm text-ink outline-none focus:border-brand"
                  required
                />
              </label>
              <div className="mt-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowNewTenantModal(false)}
                  className="rounded-lg border border-line px-3 py-1.5 text-xs font-semibold text-ink hover:bg-panel-2"
                >
                  Cancelar
                </button>
                <button
                  type="submit"
                  disabled={creatingTenant}
                  className="rounded-lg bg-brand px-4 py-1.5 text-xs font-bold text-on-brand shadow hover:brightness-105"
                >
                  {creatingTenant ? "Creando..." : "Crear"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
