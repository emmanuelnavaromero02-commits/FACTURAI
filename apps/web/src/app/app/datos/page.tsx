"use client";

import React, { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { FiscalProfileData } from "@/types/api";
import { getFiscalProfile, saveFiscalProfile } from "@/lib/api";
import { AlertTriangle, Check, Loader2, Save } from "lucide-react";

export default function DatosFiscalesPage() {
  const { activeTenant } = useAuth();
  const tenantId = activeTenant?.id;

  const defaultProfile: FiscalProfileData = {
    rfc: "NAM950812QX3",
    razon_social: "NAVA MERO S.A. DE C.V.",
    cp: "06600",
    regimen_fiscal: "626",
    uso_cfdi: "G03",
    email_receptor: "facturas@navamero.mx",
    telefono: "",
    calle: "Av. Paseo de la Reforma",
    numero_exterior: "222",
    numero_interior: "Piso 8",
    colonia: "Juárez",
    municipio_alcaldia: "Cuauhtémoc",
    estado: "Ciudad de México",
    pais: "MEX",
    curp: "",
  };

  const [form, setForm] = useState<FiscalProfileData>(defaultProfile);
  const [savedToast, setSavedToast] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Cargar datos guardados: primero desde el backend, luego fallback a localStorage
  useEffect(() => {
    if (!tenantId) return;
    let isMounted = true;

    const fetchProfile = async () => {
      try {
        setLoading(true);
        const remote = await getFiscalProfile(tenantId);
        if (remote && isMounted) {
          setForm(remote);
          localStorage.setItem(`facturia_fiscal_${tenantId}`, JSON.stringify(remote));
          return;
        }
      } catch (err) {
        console.warn("No se pudo obtener el perfil fiscal del servidor:", err);
      } finally {
        if (isMounted) setLoading(false);
      }

      // Si el servidor no devolvió perfil, intentar cargar de localStorage
      const stored = localStorage.getItem(`facturia_fiscal_${tenantId}`);
      if (stored && isMounted) {
        try {
          setForm(JSON.parse(stored));
        } catch {
          // Fallback a valores iniciales
        }
      }
    };

    fetchProfile();
    return () => {
      isMounted = false;
    };
  }, [tenantId]);

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>
  ) => {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tenantId) return;
    setSaving(true);
    setErrorMsg(null);
    try {
      const saved = await saveFiscalProfile(tenantId, form);
      setForm(saved);
      localStorage.setItem(`facturia_fiscal_${tenantId}`, JSON.stringify(saved));
      setSavedToast(true);
      setTimeout(() => setSavedToast(false), 3000);
    } catch (err: any) {
      setErrorMsg(err.message || "Error al guardar el perfil fiscal en el servidor.");
    } finally {
      setSaving(false);
    }
  };

  // Identificar campos vacíos para alertar al usuario
  const emptyFields: string[] = [];
  if (!form.rfc) emptyFields.push("RFC");
  if (!form.razon_social) emptyFields.push("Razón Social");
  if (!form.cp) emptyFields.push("Código Postal");
  if (!form.email_receptor) emptyFields.push("Correo Receptor");
  if (!form.calle) emptyFields.push("Calle");
  if (!form.numero_exterior) emptyFields.push("Número Exterior");
  if (!form.colonia) emptyFields.push("Colonia");
  if (!form.municipio_alcaldia) emptyFields.push("Municipio/Alcaldía");
  if (!form.estado) emptyFields.push("Estado");
  if (!form.telefono) emptyFields.push("Teléfono");

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-ink">
            Datos fiscales
          </h1>
          <p className="mt-0.5 text-xs text-muted">
            Lo que el agente escribe en cada portal de facturación, sin que tú lo teclees.
          </p>
        </div>
      </div>

      {/* Alerta de campos vacíos */}
      {emptyFields.length > 0 && (
        <div className="flex items-start gap-3 rounded-xl border border-warn/40 bg-warn-soft p-4 text-xs text-ink-2">
          <AlertTriangle className="h-5 w-5 shrink-0 text-warn" />
          <div className="flex-1">
            <b className="font-bold text-warn">
              {emptyFields.length} campos vacíos en la constancia fiscal
            </b>
            <p className="mt-1">
              Si el portal de un comercio requiere alguno de estos datos (ej. domicilio completo o teléfono) y no está registrado, el agente no inventará nada y el ticket se quedará en <b>espera humano</b>.
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {emptyFields.map((f) => (
                <span
                  key={f}
                  className="rounded-md border border-warn/30 bg-panel px-2 py-0.5 text-[11px] font-medium text-warn"
                >
                  {f} vacío
                </span>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Formulario Completo de la Constancia de Situación Fiscal SAT */}
      <div className="overflow-hidden rounded-2xl border border-line bg-panel shadow-prototipo">
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <h2 className="text-sm font-bold text-ink">RFC Principal del Tenant</h2>
          <span className="rounded-md border border-ok/40 bg-ok-soft px-2.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-wider text-ok">
            Constancia SAT CFDI 4.0
          </span>
        </div>

        <form onSubmit={handleSave} className="flex flex-col p-5">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {/* Razón Social */}
            <label className="sm:col-span-2">
              <span className="lt mb-1.5 block">Nombre o Razón Social (Exacta SAT)</span>
              <input
                type="text"
                name="razon_social"
                value={form.razon_social}
                onChange={handleChange}
                placeholder="Ej: EMPRESA S.A. DE C.V."
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink uppercase outline-none focus:border-brand"
                required
              />
            </label>

            {/* RFC */}
            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">RFC</span>
                {!form.rfc && (
                  <span className="text-[10px] font-bold text-warn">Requerido</span>
                )}
              </div>
              <input
                type="text"
                name="rfc"
                value={form.rfc}
                onChange={handleChange}
                maxLength={13}
                placeholder="NAM950812QX3"
                className="mono w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink uppercase outline-none focus:border-brand"
                required
              />
            </label>

            {/* Código Postal Fiscal */}
            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">C.P. Fiscal</span>
                {!form.cp && (
                  <span className="text-[10px] font-bold text-warn">Requerido</span>
                )}
              </div>
              <input
                type="text"
                name="cp"
                value={form.cp}
                onChange={handleChange}
                maxLength={5}
                placeholder="06600"
                className="mono w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
                required
              />
            </label>

            {/* Régimen Fiscal */}
            <label className="sm:col-span-2">
              <span className="lt mb-1.5 block">Régimen Fiscal (SAT)</span>
              <select
                name="regimen_fiscal"
                value={form.regimen_fiscal}
                onChange={handleChange}
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              >
                <option value="601">601 · General de Ley Personas Morales</option>
                <option value="626">626 · Régimen Simplificado de Confianza (RESICO)</option>
                <option value="612">612 · Personas Físicas con Actividades Empresariales</option>
                <option value="605">605 · Sueldos y Salarios</option>
                <option value="603">603 · Personas Morales con Fines no Lucrativos</option>
              </select>
            </label>

            {/* Uso de CFDI por defecto */}
            <label className="sm:col-span-2">
              <span className="lt mb-1.5 block">Uso del CFDI por defecto</span>
              <select
                name="uso_cfdi"
                value={form.uso_cfdi}
                onChange={handleChange}
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              >
                <option value="G03">G03 · Gastos en general</option>
                <option value="G01">G01 · Adquisición de mercancías</option>
                <option value="G02">G02 · Devoluciones, descuentos o bonificaciones</option>
                <option value="I08">I08 · Otra maquinaria y equipo</option>
                <option value="CP01">CP01 · Pagos</option>
                <option value="S01">S01 · Sin efectos fiscales</option>
              </select>
            </label>

            {/* Correo receptor */}
            <label className="sm:col-span-2">
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">Correo que recibe los CFDI</span>
                {!form.email_receptor && (
                  <span className="text-[10px] font-bold text-warn">Requerido</span>
                )}
              </div>
              <input
                type="email"
                name="email_receptor"
                value={form.email_receptor}
                onChange={handleChange}
                placeholder="facturas@tuempresa.mx"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
                required
              />
            </label>

            {/* Teléfono */}
            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">Teléfono de contacto</span>
                {!form.telefono && (
                  <span className="text-[10px] text-muted">Opcional</span>
                )}
              </div>
              <input
                type="tel"
                name="telefono"
                value={form.telefono || ""}
                onChange={handleChange}
                placeholder="55 1234 5678"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              />
            </label>

            {/* CURP */}
            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">CURP (Personas Físicas)</span>
                {!form.curp && (
                  <span className="text-[10px] text-muted">Opcional</span>
                )}
              </div>
              <input
                type="text"
                name="curp"
                value={form.curp || ""}
                onChange={handleChange}
                maxLength={18}
                placeholder="ABCD950812HDF..."
                className="mono w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink uppercase outline-none focus:border-brand"
              />
            </label>

            {/* Domicilio Fiscal */}
            <div className="sm:col-span-2 border-t border-line pt-3 mt-1">
              <p className="lt mb-2">Domicilio Fiscal (Obligatorio en algunos portales)</p>
            </div>

            <label className="sm:col-span-2">
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">Calle</span>
                {!form.calle && (
                  <span className="text-[10px] font-bold text-warn">Vacío</span>
                )}
              </div>
              <input
                type="text"
                name="calle"
                value={form.calle || ""}
                onChange={handleChange}
                placeholder="Av. Paseo de la Reforma"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              />
            </label>

            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">No. Exterior</span>
                {!form.numero_exterior && (
                  <span className="text-[10px] font-bold text-warn">Vacío</span>
                )}
              </div>
              <input
                type="text"
                name="numero_exterior"
                value={form.numero_exterior || ""}
                onChange={handleChange}
                placeholder="123"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              />
            </label>

            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">No. Interior</span>
                {!form.numero_interior && (
                  <span className="text-[10px] text-muted">Opcional</span>
                )}
              </div>
              <input
                type="text"
                name="numero_interior"
                value={form.numero_interior || ""}
                onChange={handleChange}
                placeholder="Piso 4, Int. B"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              />
            </label>

            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">Colonia</span>
                {!form.colonia && (
                  <span className="text-[10px] font-bold text-warn">Vacío</span>
                )}
              </div>
              <input
                type="text"
                name="colonia"
                value={form.colonia || ""}
                onChange={handleChange}
                placeholder="Juárez"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              />
            </label>

            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">Municipio / Alcaldía</span>
                {!form.municipio_alcaldia && (
                  <span className="text-[10px] font-bold text-warn">Vacío</span>
                )}
              </div>
              <input
                type="text"
                name="municipio_alcaldia"
                value={form.municipio_alcaldia || ""}
                onChange={handleChange}
                placeholder="Cuauhtémoc"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              />
            </label>

            <label>
              <div className="flex items-center justify-between mb-1.5">
                <span className="lt">Estado / Entidad</span>
                {!form.estado && (
                  <span className="text-[10px] font-bold text-warn">Vacío</span>
                )}
              </div>
              <input
                type="text"
                name="estado"
                value={form.estado || ""}
                onChange={handleChange}
                placeholder="Ciudad de México"
                className="w-full rounded-xl border border-line-2 bg-panel-2 p-2.5 text-xs text-ink outline-none focus:border-brand"
              />
            </label>

            <label>
              <span className="lt mb-1.5 block">País</span>
              <input
                type="text"
                name="pais"
                value={form.pais}
                onChange={handleChange}
                readOnly
                className="w-full rounded-xl border border-line-2 bg-panel-3 p-2.5 text-xs text-muted outline-none"
              />
            </label>
          </div>

          {errorMsg && (
            <div className="mt-4 rounded-xl border border-bad/30 bg-bad-soft p-3 text-xs text-bad">
              <b>Error:</b> {errorMsg}
            </div>
          )}

          <div className="mt-6 flex items-center justify-between border-t border-line pt-4">
            <p className="text-[11px] text-muted">
              Sincronizado de forma cifrada en la base de datos PostgreSQL bajo aislamiento de tenant (RLS).
            </p>
            <button
              type="submit"
              disabled={saving}
              className="flex items-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-xs font-bold text-on-brand shadow transition-all hover:brightness-105 active:scale-98 disabled:opacity-50"
            >
              {saving ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Save className="h-4 w-4" />
              )}
              <span>{saving ? "Guardando..." : "Guardar datos fiscales"}</span>
            </button>
          </div>
        </form>
      </div>

      {savedToast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-2 rounded-full bg-ink px-4 py-2 text-xs font-semibold text-bg shadow-xl animate-slide-in">
          <Check className="h-4 w-4 text-ok" />
          <span>Datos fiscales actualizados correctamente</span>
        </div>
      )}
    </div>
  );
}
