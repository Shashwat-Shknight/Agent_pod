/**
 * The only way the UI talks to the server.
 *
 * Handshake contract with FastAPI (app/web/errors.py):
 *  - every failure is JSON {error: {code, message, fields?}}; anything else (proxy page, crash) becomes a clear ApiError
 *  - state-changing calls carry X-NuuCare-Client (CSRF guard) and the HttpOnly session cookie
 *  - 401 tells the auth layer to show sign-in (with the server's reason, e.g. idle timeout)
 *  - every call has a timeout; AI calls get a long one because the LLM gateway retries internally
 */

export type ApiErrorCode =
  | "network"
  | "timeout"
  | "bad_response"
  | "unauthenticated"
  | "session_expired"
  | "stale_record"
  | "password_change_required"
  | string;

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly fields: Record<string, string>;

  constructor(status: number, code: ApiErrorCode, message: string, fields: Record<string, string> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
  }

  get isConflict() {
    return this.status === 409 && this.code === "stale_record";
  }
}

export const TIMEOUTS = {
  normal: 30_000,
  ai: 300_000, // scribe / CDS: the gateway retries up to 5x before answering
  upload: 300_000,
};

type Listener = (error: ApiError) => void;
const authListeners = new Set<Listener>();

/** The auth provider subscribes so any 401 anywhere returns the user to sign-in. */
export function onAuthError(listener: Listener): () => void {
  authListeners.add(listener);
  return () => authListeners.delete(listener);
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  form?: FormData;
  timeout?: number;
  signal?: AbortSignal;
  /** Auth endpoints handle 401 themselves (wrong password is not "signed out"). */
  silentAuth?: boolean;
}

async function parseError(response: Response): Promise<ApiError> {
  let payload: unknown = null;
  const text = await response.text().catch(() => "");
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  const error = (payload as { error?: { code?: string; message?: string; fields?: Record<string, string> } } | null)?.error;
  if (error?.message) {
    return new ApiError(response.status, error.code ?? `http_${response.status}`, error.message, error.fields ?? {});
  }
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return new ApiError(response.status, `http_${response.status}`, detail);
  if (response.status === 502 || response.status === 504) {
    return new ApiError(response.status, "server_unreachable", "The NuuCare server is not responding. Check that it is running.");
  }
  return new ApiError(response.status, "bad_response", `The server answered with an unexpected response (HTTP ${response.status}).`);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? (options.body !== undefined || options.form ? "POST" : "GET");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort("timeout"), options.timeout ?? TIMEOUTS.normal);
  options.signal?.addEventListener("abort", () => controller.abort("cancelled"), { once: true });

  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-NuuCare-Client"] = "web";
  let body: BodyInit | undefined;
  if (options.form) {
    body = options.form; // the browser sets the multipart boundary
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(path, { method, headers, body, credentials: "same-origin", signal: controller.signal });
  } catch (cause) {
    window.clearTimeout(timeout);
    if (controller.signal.aborted && controller.signal.reason === "timeout") {
      throw new ApiError(0, "timeout", "The server took too long to answer. Your work was not lost; try again.");
    }
    if (controller.signal.aborted) throw new ApiError(0, "cancelled", "Request cancelled.");
    throw new ApiError(0, "network", "Cannot reach the NuuCare server. Check that it is running and you are online.");
  }
  window.clearTimeout(timeout);

  if (!response.ok) {
    const error = await parseError(response);
    if (response.status === 401 && !options.silentAuth) authListeners.forEach((listener) => listener(error));
    if (error.code === "password_change_required") authListeners.forEach((listener) => listener(error));
    throw error;
  }
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") ?? "";
  if (!type.includes("application/json")) {
    throw new ApiError(response.status, "bad_response", "The server returned a page instead of data. Is the API running?");
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T,>(path: string, timeout?: number) => request<T>(path, { timeout }),
  post: <T,>(path: string, body: unknown = {}, timeout?: number) => request<T>(path, { method: "POST", body, timeout }),
  put: <T,>(path: string, body: unknown, timeout?: number) => request<T>(path, { method: "PUT", body, timeout }),
  patch: <T,>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body }),
  upload: <T,>(path: string, form: FormData) => request<T>(path, { method: "POST", form, timeout: TIMEOUTS.upload }),
};

/** Human message for any thrown value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

/** Download a binary (PDF) with the same cookie, surfacing JSON errors properly. */
export async function download(path: string, fallbackName: string): Promise<void> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? fallbackName;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
