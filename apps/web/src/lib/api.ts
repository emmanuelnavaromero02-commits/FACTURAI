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
    // Si estamos en el navegador en un celular/otra máquina en LAN, apuntar al mismo host en el puerto 8000
    if (window.location.hostname !== "localhost" && window.location.hostname !== "127.0.0.1") {
      return `http://${window.location.hostname}:8000`;
    }
  }
  return process.env.NEXT_PUBLIC_API_URL?.replace(/\/+$/, "") || "http://localhost:8000";
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

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 8000);

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
      throw new ApiError(408, "tiempo_agotado", "Tiempo de espera agotado al conectar con el servidor.");
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
  estado?: string
): Promise<PaginatedTicketsResponse> {
  const query = estado ? `?estado=${encodeURIComponent(estado)}` : "";
  return fetchWithAuth<PaginatedTicketsResponse>(`/v1/tickets${query}`, {}, tenantId);
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
