"use client";

import React, { createContext, useContext, useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { getMe, loginWithGoogle, logout } from "./api";
import { MeResponse, UserTenantResponse } from "../types/api";

interface AuthContextType {
  user: MeResponse | null;
  loading: boolean;
  activeTenant: UserTenantResponse | null;
  setActiveTenantId: (tenantId: string) => void;
  loginDevMode: (email?: string) => Promise<void>;
  logoutUser: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<MeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTenantId, setActiveTenantIdState] = useState<string | null>(null);
  const router = useRouter();
  const pathname = usePathname();

  const refreshUser = async () => {
    try {
      const me = await getMe();
      setUser(me);

      // Si no hay tenant seleccionado o el actual ya no existe, tomar el primero
      if (me.tenants.length > 0) {
        const savedTenantId = typeof window !== "undefined" ? localStorage.getItem("facturia_tenant_id") : null;
        const exists = me.tenants.find((t) => t.id === savedTenantId);
        const chosenId = exists ? exists.id : me.tenants[0].id;
        setActiveTenantIdState(chosenId);
        if (typeof window !== "undefined") {
          localStorage.setItem("facturia_tenant_id", chosenId);
        }
      }
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refreshUser();
    // Seguro de vida: garantiza que el spinner nunca quede congelado indefinidamente
    const safetyTimer = setTimeout(() => {
      setLoading(false);
    }, 3000);
    return () => clearTimeout(safetyTimer);
  }, []);

  const setActiveTenantId = (tenantId: string) => {
    setActiveTenantIdState(tenantId);
    if (typeof window !== "undefined") {
      localStorage.setItem("facturia_tenant_id", tenantId);
    }
  };

  const loginDevMode = async (email = "emmanuel@navamero.mx") => {
    setLoading(true);
    try {
      await loginWithGoogle(`mock:${email}`);
      await refreshUser();
      router.push("/app");
    } finally {
      setLoading(false);
    }
  };

  const logoutUser = async () => {
    setLoading(true);
    try {
      await logout();
      setUser(null);
      if (typeof window !== "undefined") {
        localStorage.removeItem("facturia_tenant_id");
      }
      router.push("/login");
    } finally {
      setLoading(false);
    }
  };

  const activeTenant =
    user?.tenants.find((t) => t.id === activeTenantId) ||
    user?.tenants[0] ||
    null;

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        activeTenant,
        setActiveTenantId,
        loginDevMode,
        logoutUser,
        refreshUser,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth debe ser usado dentro de un AuthProvider");
  }
  return context;
}
