"use client";

import React, { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { getTeam, inviteTeamMember, updateTeamMemberRole } from "@/lib/api";
import { MembershipRole, TeamMemberResponse } from "@/types/api";
import { Plus, UserPlus, Shield, X, Check } from "lucide-react";

export default function EquipoPage() {
  const { activeTenant } = useAuth();
  const tenantId = activeTenant?.id;

  const [members, setMembers] = useState<TeamMemberResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [showInviteModal, setShowInviteModal] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteName, setInviteName] = useState("");
  const [inviteRole, setInviteRole] = useState<MembershipRole>("uploader");
  const [inviting, setInviting] = useState(false);
  const [toastMsg, setToastMsg] = useState<string | null>(null);

  const loadTeam = async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const data = await getTeam(tenantId);
      setMembers(data || []);
    } catch (err) {
      console.error("Error al cargar equipo:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTeam();
  }, [tenantId]);

  const showToast = (msg: string) => {
    setToastMsg(msg);
    setTimeout(() => setToastMsg(null), 2500);
  };

  const handleInviteSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tenantId || !inviteEmail.trim()) return;

    setInviting(true);
    try {
      await inviteTeamMember(
        tenantId,
        inviteEmail.trim(),
        inviteRole,
        inviteName.trim() || undefined
      );
      showToast(`Invitación enviada a ${inviteEmail}`);
      setShowInviteModal(false);
      setInviteEmail("");
      setInviteName("");
      setInviteRole("uploader");
      await loadTeam();
    } catch (err: unknown) {
      alert((err as Error).message || "No se pudo enviar la invitación.");
    } finally {
      setInviting(false);
    }
  };

  const handleRoleChange = async (userId: string, newRole: MembershipRole) => {
    if (!tenantId) return;
    try {
      await updateTeamMemberRole(tenantId, userId, newRole);
      showToast("Rol actualizado exitosamente");
      await loadTeam();
    } catch (err: unknown) {
      alert((err as Error).message || "No tienes permisos para modificar este rol.");
    }
  };

  const roleLabels: Record<string, string> = {
    owner: "Owner / Dueño",
    admin: "Administrador",
    uploader: "Sube tickets",
    viewer: "Solo lectura",
  };

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-ink">Equipo</h1>
          <p className="mt-0.5 text-xs text-muted">
            Quién puede subir tickets y quién ve toda la información fiscal.
          </p>
        </div>

        <button
          type="button"
          onClick={() => setShowInviteModal(true)}
          className="flex items-center gap-2 rounded-xl border border-line-2 bg-panel px-4 py-2.5 text-xs font-bold text-ink shadow-sm hover:border-brand hover:text-brand"
        >
          <Plus className="h-4 w-4" />
          <span>Invitar miembro</span>
        </button>
      </div>

      {/* Tarjeta de Lista de Miembros */}
      <div className="overflow-hidden rounded-2xl border border-line bg-panel shadow-prototipo">
        <div className="border-b border-line px-5 py-3.5">
          <h2 className="text-sm font-bold text-ink">
            Miembros de {activeTenant?.nombre || "la Empresa"}
          </h2>
        </div>

        <div className="divide-y divide-line">
          {members.map((m) => {
            const initials = (m.nombre || m.email)
              .split(" ")
              .map((w) => w[0])
              .slice(0, 2)
              .join("")
              .toUpperCase();

            return (
              <div
                key={m.user_id}
                className="flex items-center justify-between gap-3 p-4 transition-colors hover:bg-panel-2"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-line bg-panel-2 text-xs font-bold text-ink-2">
                    {initials}
                  </span>
                  <div className="min-w-0">
                    <b className="block truncate text-sm font-semibold text-ink">
                      {m.nombre || m.email.split("@")[0]}
                    </b>
                    <span className="block truncate text-xs text-muted">
                      {m.email}
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <select
                    value={m.rol}
                    onChange={(e) =>
                      handleRoleChange(m.user_id, e.target.value as MembershipRole)
                    }
                    className="rounded-lg border border-line-2 bg-panel-2 px-2.5 py-1 text-xs font-semibold text-ink-2 outline-none hover:border-line"
                  >
                    <option value="owner">Owner</option>
                    <option value="admin">Administrador</option>
                    <option value="uploader">Sube tickets</option>
                    <option value="viewer">Solo lectura</option>
                  </select>
                </div>
              </div>
            );
          })}

          {!loading && members.length === 0 && (
            <div className="p-8 text-center text-xs text-muted">
              No se encontraron miembros para este equipo.
            </div>
          )}
        </div>
      </div>

      {/* Nota de Aislamiento Multi-tenant (docs/prototipo.html:331-333) */}
      <div>
        <p className="lt mb-2">Aislamiento de Seguridad</p>
        <div className="rounded-xl border border-line bg-panel-2 p-4 text-xs leading-relaxed text-ink-2">
          Cada cuenta es un <b>tenant</b> independiente: sus tickets, sus RFC y sus credenciales de portal viven en su propio espacio y ninguna consulta puede cruzar de una a otra (reforzado con Row-Level Security en PostgreSQL). Un usuario invitado a dos empresas cambia entre ellas con el selector de arriba a la izquierda.
        </div>
      </div>

      {/* Modal Invitar Miembro */}
      {showInviteModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-md rounded-2xl border border-line bg-panel p-5 shadow-2xl">
            <div className="mb-4 flex items-start justify-between">
              <div>
                <h3 className="text-base font-bold text-ink">Invitar al equipo</h3>
                <p className="text-xs text-muted">
                  Agrega a un colaborador a {activeTenant?.nombre}.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowInviteModal(false)}
                className="flex h-7 w-7 items-center justify-center rounded-lg bg-panel-2 text-muted hover:text-ink"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <form onSubmit={handleInviteSubmit} className="flex flex-col gap-3.5">
              <label className="text-xs font-semibold text-ink">
                Correo electrónico:
                <input
                  type="email"
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  placeholder="colaborador@empresa.mx"
                  className="mt-1 w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
                  required
                />
              </label>

              <label className="text-xs font-semibold text-ink">
                Nombre (opcional):
                <input
                  type="text"
                  value={inviteName}
                  onChange={(e) => setInviteName(e.target.value)}
                  placeholder="Nombre de la persona"
                  className="mt-1 w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
                />
              </label>

              <label className="text-xs font-semibold text-ink">
                Rol en el equipo:
                <select
                  value={inviteRole}
                  onChange={(e) => setInviteRole(e.target.value as MembershipRole)}
                  className="mt-1 w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
                >
                  <option value="uploader">Sube tickets (Subir fotos y ver tickets propios)</option>
                  <option value="viewer">Solo lectura (Ver reportes y facturas)</option>
                  <option value="admin">Administrador (Gestionar equipo y datos fiscales)</option>
                  <option value="owner">Owner (Control total del tenant)</option>
                </select>
              </label>

              <div className="mt-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowInviteModal(false)}
                  className="rounded-xl border border-line px-4 py-2 text-xs font-semibold text-ink hover:bg-panel-2"
                >
                  Cancelar
                </button>
                <button
                  type="submit"
                  disabled={inviting}
                  className="flex items-center gap-1.5 rounded-xl bg-brand px-4 py-2 text-xs font-bold text-on-brand shadow hover:brightness-105"
                >
                  <UserPlus className="h-3.5 w-3.5" />
                  <span>{inviting ? "Enviando..." : "Invitar"}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {toastMsg && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-2 rounded-full bg-ink px-4 py-2 text-xs font-semibold text-bg shadow-xl animate-slide-in">
          <Check className="h-4 w-4 text-ok" />
          <span>{toastMsg}</span>
        </div>
      )}
    </div>
  );
}
