// The typed API client and the session it carries.
//
// Token handling, and why it looks like this:
//
// - The access token lives in this module's memory only. It is short-lived
//   (15 minutes) and never written to storage, so a script injected into the
//   page cannot lift a long-lived credential from localStorage.
// - The refresh token never reaches JavaScript at all. The API sets it as an
//   httpOnly, SameSite=Strict cookie scoped to `/api/auth` (login with
//   `transport: "cookie"`), and the browser presents it on refresh.
// - A reload loses the access token; `restoreSession()` trades the cookie for a
//   new one on boot, so the user stays signed in across reloads.
// - Any 401 from a non-auth endpoint triggers one refresh and one retry. The
//   refresh is single-flight: ten queries failing together cause one refresh,
//   not ten - which matters, because the server treats a reused refresh token
//   as theft and revokes every session of that user.
// - If the refresh itself fails, the session is over: listeners are told, and
//   the app sends the user to the login page with their place remembered.

import createClient from "openapi-fetch";

import type { Paths, TokenOut, User } from "./types";

export const API_BASE = "/api";

// --- session state -----------------------------------------------------------

type SessionEvent = "expired";

let accessToken: string | null = null;
let expiresAt = 0;
let inflight: Promise<boolean> | null = null;
const listeners = new Set<(event: SessionEvent) => void>();

export function onSessionEvent(listener: (event: SessionEvent) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function setToken(token: TokenOut): void {
  accessToken = token.access_token;
  expiresAt = Date.parse(token.expires_at);
}

export function clearSession(): void {
  accessToken = null;
  expiresAt = 0;
}

export function hasSession(): boolean {
  return accessToken !== null;
}

/** Exchange the refresh cookie for a new access token. Single-flight. */
export function refreshSession(): Promise<boolean> {
  inflight ??= (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        clearSession();
        return false;
      }
      setToken((await response.json()) as TokenOut);
      return true;
    } catch {
      clearSession();
      return false;
    } finally {
      // Released after the promise settles, so callers already awaiting it
      // share the result and the next 401 starts a fresh refresh.
      queueMicrotask(() => {
        inflight = null;
      });
    }
  })();
  return inflight;
}

const isAuthCall = (url: string) => /\/api\/auth\/(login|refresh|logout)$/.test(new URL(url).pathname);

/** Refresh a little before expiry, so a request is not sent with a dying token. */
const EXPIRY_MARGIN_MS = 30_000;

async function authFetch(request: Request): Promise<Response> {
  if (isAuthCall(request.url)) return fetch(request);

  if (accessToken && Date.now() > expiresAt - EXPIRY_MARGIN_MS) await refreshSession();
  const retry = request.clone();
  if (accessToken) request.headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(request);
  if (response.status !== 401) return response;

  const refreshed = await refreshSession();
  if (!refreshed) {
    for (const listener of listeners) listener("expired");
    return response;
  }
  retry.headers.set("Authorization", `Bearer ${accessToken}`);
  return fetch(retry);
}

export const api = createClient<Paths>({ baseUrl: API_BASE, fetch: authFetch });

// --- errors ------------------------------------------------------------------

/** The API's error envelope, thrown so TanStack Query sees a failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details ?? {};
  }
}

interface Envelope {
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

export function toApiError(response: Response, body: unknown): ApiError {
  const error = (body as Envelope | undefined)?.error;
  return new ApiError(
    response.status,
    error?.code ?? `http_${response.status}`,
    error?.message ?? (response.statusText || "The request failed"),
    error?.details,
  );
}

/** Await an openapi-fetch call; return its data or throw its error envelope. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await call;
  } catch (cause) {
    throw new ApiError(0, "network_error", "The server could not be reached. Is the API running?", {
      cause: String(cause),
    });
  }
  if (result.error !== undefined || !result.response.ok) throw toApiError(result.response, result.error);
  return result.data as T;
}

// --- uploads -------------------------------------------------------------------

/**
 * POST a multipart form with upload progress.
 *
 * XMLHttpRequest rather than fetch, because fetch reports no upload progress
 * and a 25 MB workbook over a slow link deserves a progress bar. Same
 * refresh-once-on-401 rule as every other call.
 */
export async function uploadForm<T>(
  path: string,
  form: FormData,
  onProgress: (fraction: number) => void,
): Promise<T> {
  const send = () =>
    new Promise<{ status: number; body: unknown; statusText: string }>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_BASE}${path}`);
      xhr.responseType = "json";
      if (accessToken) xhr.setRequestHeader("Authorization", `Bearer ${accessToken}`);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      xhr.onload = () => resolve({ status: xhr.status, body: xhr.response, statusText: xhr.statusText });
      xhr.onerror = () => reject(new ApiError(0, "network_error", "The upload could not reach the server."));
      xhr.send(form);
    });

  if (accessToken && Date.now() > expiresAt - EXPIRY_MARGIN_MS) await refreshSession();
  let result = await send();
  if (result.status === 401 && (await refreshSession())) result = await send();
  if (result.status < 200 || result.status >= 300) {
    throw toApiError(new Response(null, { status: result.status, statusText: result.statusText }), result.body);
  }
  onProgress(1);
  return result.body as T;
}

// --- session operations --------------------------------------------------------

export async function login(email: string, password: string): Promise<User> {
  const token = await unwrap(api.POST("/auth/login", { body: { email, password, transport: "cookie" } }));
  setToken(token);
  return unwrap(api.GET("/auth/me"));
}

export async function restoreSession(): Promise<User | null> {
  if (!(await refreshSession())) return null;
  try {
    return await unwrap(api.GET("/auth/me"));
  } catch {
    clearSession();
    return null;
  }
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "same-origin" });
  } finally {
    clearSession();
  }
}
