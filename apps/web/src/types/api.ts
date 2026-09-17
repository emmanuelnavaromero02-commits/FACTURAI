/**
 * Tipos generados a partir de OpenAPI de Facturia API
 * NO MODIFICAR MANUALMENTE campos que la API no devuelva.
 */

export type TipoMotor = "api" | "email" | "web" | "manual";

export type MembershipRole = "owner" | "admin" | "uploader" | "viewer";

export type TicketEstado =
  | "recibido"
  | "extrayendo"
  | "extraido"
  | "encolado"
  | "facturando"
  | "espera_humano"
  | "facturado"
  | "rechazado"
  | "cancelado";

export interface GoogleAuthRequest {
  id_token: string;
}

export interface GoogleAuthResponse {
  user_id: string;
  email: string;
  nombre: string;
  avatar_url?: string | null;
  created_tenant_id?: string | null;
}

export interface UserTenantResponse {
  id: string;
  nombre: string;
  slug: string;
  plan: string;
  rol: MembershipRole | string;
}

export interface MeResponse {
  id: string;
  email: string;
  nombre: string;
  avatar_url?: string | null;
  tenants: UserTenantResponse[];
}

export interface TenantResponse {
  id: string;
  nombre: string;
  slug: string;
  plan: string;
  rol: string;
}

export interface CreateTenantRequest {
  nombre: string;
  slug?: string | null;
}

export interface TeamMemberResponse {
  user_id: string;
  nombre: string;
  email: string;
  rol: string;
  avatar_url?: string | null;
}

export interface InviteMemberRequest {
  email: string;
  nombre?: string | null;
  rol: MembershipRole;
}

export interface UpdateMemberRoleRequest {
  rol: MembershipRole;
}

export interface TicketResponse {
  id: string;
  tenant_id: string;
  estado: TicketEstado | string;
  intentos: number;
  folio?: string | null;
  web_id?: string | null;
  fecha_ticket?: string | null;
  hora_ticket?: string | null;
  total?: string | null;
  subtotal?: string | null;
  iva?: string | null;
  sucursal?: string | null;
  caja?: string | null;
  rfc_emisor?: string | null;
  confianza?: string | null;
  error_code?: string | null;
  error_msg?: string | null;
  image_key?: string | null;
  image_deleted_at?: string | null;
  cfdi_uuid?: string | null;
  correo_capturado_en_portal?: string | null;
  cfdi_disponible_hasta?: string | null;
  url_facturacion?: string | null;
  costo_total_usd?: string | null;
  pasos_agente?: number | null;
  duracion_segundos?: number | null;
  categoria_gasto?: string | null;
  desglose_impuestos?: {
    base_16?: number;
    iva_16?: number;
    base_0?: number;
    iva_0?: number;
    base_exenta?: number;
    ieps?: number;
    ish?: number;
    tua?: number;
    retencion_iva?: number;
    retencion_isr?: number;
    fuente?: string;
  } | null;
  estatus_deducibilidad?: string | null;
  score_riesgo_fiscal?: number | null;
  auditoria_aritmetica?: {
    es_valido_anexo_20: boolean;
    score_matematico: number;
    total_calculado: number;
    total_declarado: number;
    discrepancia: number;
    tolerancia_permitida: number;
    tasa_efectiva_iva?: number | null;
    alertas?: string[];
  } | null;
  hash_integridad?: string | null;
  created_at: string;
}

export interface PaginatedTicketsResponse {
  items: TicketResponse[];
  next_cursor?: string | null;
}

export interface TicketEventPayload {
  id: number;
  ticket_id: string;
  tipo: string;
  mensaje: string;
  ts: string;
  meta?: Record<string, unknown> | null;
}

export interface FiscalProfileData {
  rfc: string;
  razon_social: string;
  cp: string;
  regimen_fiscal: string;
  uso_cfdi: string;
  email_receptor: string;
  telefono?: string;
  calle?: string;
  numero_exterior?: string;
  numero_interior?: string;
  colonia?: string;
  municipio_alcaldia?: string;
  estado?: string;
  pais: string;
  curp?: string;
  es_principal?: boolean;
}
