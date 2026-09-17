import {
  GoogleAuthResponse,
  MeResponse,
  PaginatedTicketsResponse,
  TeamMemberResponse,
  TenantResponse,
  TicketResponse,
  FiscalProfileData,
} from "../types/api";

function getApiBaseUrl(): string {
  if (typeof window !== "undefined") {
    // En el navegador, usar la ruta relativa /v1/... a través del reverse proxy de Next.js
    // Esto funciona 100% en localhost, IP de red local (celulares) y túneles externos.
    return "";
  }
  return process.env.NEXT_PUBLIC_API_URL?.replace(/\/+$/, "") || "http://127.0.0.1:8000";
}

export const API_BASE_URL = getApiBaseUrl();

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail?: unknown
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function fetchWithAuth<T>(
  endpoint: string,
  options: RequestInit = {},
  tenantId?: string | null
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  const headers = new Headers(options.headers || {});

  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  if (tenantId) {
    headers.set("X-Tenant-Id", tenantId);
  }

  const isUpload = options.body instanceof FormData;
  // Subidas de archivos en redes móviles necesitan hasta 60s, peticiones normales 20s
  const defaultTimeout = isUpload ? 60000 : 20000;
  const timeoutDuration = (options as any).timeoutMs || defaultTimeout;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutDuration);

  let response: Response;
  try {
    response = await fetch(url, {
      ...options,
      headers,
      credentials: "include", // Permite el transporte de la cookie HttpOnly facturia_session
      signal: options.signal || controller.signal,
    });
  } catch (err: unknown) {
    if ((err as Error).name === "AbortError") {
      throw new ApiError(
        408,
        "tiempo_agotado",
        isUpload
          ? "El archivo tardó demasiado en subirse. Revisa tu conexión de red o reintenta la subida."
          : "Tiempo de espera agotado al conectar con el servidor."
      );
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }

  if (!response.ok) {
    let errorData: any = {};
    try {
      errorData = await response.json();
    } catch {
      // Ignorar fallo de parseo JSON
    }

    const errObj = errorData.error || errorData;
    const code = errObj.code || errorData.error_code || `HTTP_${response.status}`;
    const msg =
      errObj.message ||
      errorData.message ||
      (typeof errObj.detail === "string"
        ? errObj.detail
        : typeof errorData.detail === "string"
        ? errorData.detail
        : `Error en la solicitud (${response.status})`);

    throw new ApiError(response.status, code, msg, errObj.detail || errorData.detail);
  }

  if (response.status === 204) {
    return null as T;
  }

  return response.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export async function loginWithGoogle(idToken: string): Promise<GoogleAuthResponse> {
  return fetchWithAuth<GoogleAuthResponse>("/v1/auth/google", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: idToken }),
  });
}

export async function logout(): Promise<{ ok: boolean }> {
  return fetchWithAuth<{ ok: boolean }>("/v1/auth/logout", {
    method: "POST",
  });
}

export async function getMe(): Promise<MeResponse> {
  return fetchWithAuth<MeResponse>("/v1/me");
}

// ---------------------------------------------------------------------------
// Tenants
// ---------------------------------------------------------------------------

export async function getTenants(): Promise<TenantResponse[]> {
  return fetchWithAuth<TenantResponse[]>("/v1/tenants");
}

export async function createTenant(nombre: string, slug?: string): Promise<TenantResponse> {
  return fetchWithAuth<TenantResponse>("/v1/tenants", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nombre, slug }),
  });
}

// ---------------------------------------------------------------------------
// Tickets
// ---------------------------------------------------------------------------

export async function getTickets(
  tenantId: string,
  estado?: string,
  categoria?: string
): Promise<PaginatedTicketsResponse> {
  const params = new URLSearchParams();
  if (estado) params.set("estado", estado);
  if (categoria && categoria !== "todos") params.set("categoria", categoria);
  const qStr = params.toString() ? `?${params.toString()}` : "";
  return fetchWithAuth<PaginatedTicketsResponse>(`/v1/tickets${qStr}`, {}, tenantId);
}

export async function getTicket(tenantId: string, ticketId: string): Promise<TicketResponse> {
  return fetchWithAuth<TicketResponse>(`/v1/tickets/${ticketId}`, {}, tenantId);
}

export async function uploadTicket(
  tenantId: string,
  file: File,
  merchantSlug?: string
): Promise<TicketResponse> {
  const formData = new FormData();
  formData.append("file", file);
  if (merchantSlug) {
    formData.append("merchant_slug", merchantSlug);
  }

  return fetchWithAuth<TicketResponse>(
    "/v1/tickets",
    {
      method: "POST",
      body: formData,
    },
    tenantId
  );
}

export async function uploadCfdiDirect(
  tenantId: string,
  xmlFile: File,
  pdfFile?: File
): Promise<TicketResponse> {
  const formData = new FormData();
  formData.append("xml_file", xmlFile);
  if (pdfFile) {
    formData.append("pdf_file", pdfFile);
  }

  return fetchWithAuth<TicketResponse>(
    "/v1/tickets/upload-cfdi",
    {
      method: "POST",
      body: formData,
    },
    tenantId
  );
}

export async function retryTicket(tenantId: string, ticketId: string): Promise<TicketResponse> {
  return fetchWithAuth<TicketResponse>(
    `/v1/tickets/${ticketId}/retry`,
    {
      method: "POST",
    },
    tenantId
  );
}

export async function facturarTicket(tenantId: string, ticketId: string): Promise<TicketResponse> {
  return fetchWithAuth<TicketResponse>(
    `/v1/tickets/${ticketId}/facturar`,
    {
      method: "POST",
    },
    tenantId
  );
}

export async function deleteTicket(tenantId: string, ticketId: string): Promise<void> {
  return fetchWithAuth<void>(
    `/v1/tickets/${ticketId}`,
    {
      method: "DELETE",
    },
    tenantId
  );
}

export async function updateTicketPortal(
  tenantId: string,
  ticketId: string,
  urlFacturacion: string,
  facturarAhora: boolean = true
): Promise<TicketResponse> {
  return fetchWithAuth<TicketResponse>(
    `/v1/tickets/${ticketId}/portal`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url_facturacion: urlFacturacion,
        facturar_ahora: facturarAhora,
      }),
    },
    tenantId
  );
}

export async function deduceTicketPortal(
  tenantId: string,
  ticketId: string
): Promise<{
  ticket_id: string;
  candidates: string[];
  deduced_url: string | null;
  url_facturacion_actual: string | null;
}> {
  return fetchWithAuth(
    `/v1/tickets/${ticketId}/deduce-portal`,
    {
      method: "POST",
    },
    tenantId
  );
}

export function getCfdiDownloadUrl(ticketId: string, tenantId?: string): string {
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  return `${API_BASE_URL}/v1/tickets/${ticketId}/cfdi${query}`;
}

export function getCfdiPdfDownloadUrl(ticketId: string, tenantId?: string): string {
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  return `${API_BASE_URL}/v1/tickets/${ticketId}/cfdi/pdf${query}`;
}

export function getCfdiXmlDownloadUrl(ticketId: string, tenantId?: string): string {
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  return `${API_BASE_URL}/v1/tickets/${ticketId}/cfdi/xml${query}`;
}

export interface HandoffInfoResponse {
  handoff_id: string;
  token: string;
  session_token?: string;
  motivo: string;
  expires_at: string;
  url_facturacion?: string;
  is_expired?: boolean;
}

export async function getTicketHandoffInfo(
  tenantId: string,
  ticketId: string
): Promise<HandoffInfoResponse> {
  return fetchWithAuth<HandoffInfoResponse>(
    `/v1/tickets/${ticketId}/handoff`,
    {},
    tenantId
  );
}

export function getHandoffWsUrl(
  handoffId: string,
  token: string,
  sessionToken?: string
): string {
  const base = getApiBaseUrl();
  const wsProto = base.startsWith("https") ? "wss:" : "ws:";
  const host = base.replace(/^https?:\/\//, "");
  let url = `${wsProto}//${host}/v1/handoffs/${handoffId}/live?token=${encodeURIComponent(token)}`;
  if (sessionToken) {
    url += `&session_token=${encodeURIComponent(sessionToken)}`;
  }
  return url;
}

// ---------------------------------------------------------------------------
// Team
// ---------------------------------------------------------------------------

export async function getTeam(tenantId: string): Promise<TeamMemberResponse[]> {
  return fetchWithAuth<TeamMemberResponse[]>("/v1/team", {}, tenantId);
}

export async function inviteTeamMember(
  tenantId: string,
  email: string,
  rol: string,
  nombre?: string
): Promise<TeamMemberResponse> {
  return fetchWithAuth<TeamMemberResponse>(
    "/v1/team/invitations",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, rol, nombre }),
    },
    tenantId
  );
}

export async function updateTeamMemberRole(
  tenantId: string,
  userId: string,
  rol: string
): Promise<TeamMemberResponse> {
  return fetchWithAuth<TeamMemberResponse>(
    `/v1/team/${userId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rol }),
    },
    tenantId
  );
}

// ---------------------------------------------------------------------------
// Fiscal Profile
// ---------------------------------------------------------------------------

export async function getFiscalProfile(tenantId: string): Promise<FiscalProfileData | null> {
  return fetchWithAuth<FiscalProfileData | null>("/v1/fiscal-profile", {}, tenantId);
}

export async function saveFiscalProfile(
  tenantId: string,
  data: FiscalProfileData
): Promise<FiscalProfileData> {
  return fetchWithAuth<FiscalProfileData>(
    "/v1/fiscal-profile",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
    tenantId
  );
}
