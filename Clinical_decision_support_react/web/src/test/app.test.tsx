/**
 * Smoke-renders every screen against real responses captured from a live server (fixtures.json),
 * and checks the API client's handling of the server's error contract.
 */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { api, ApiError, onAuthError, request } from "../api/client";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/toast";
import { encodeWav } from "../lib/wav";
import fixtures from "./fixtures.json";

const responses = fixtures.responses as Record<string, unknown>;
const RID = fixtures.record_id;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function mockServer(overrides: Record<string, Response | (() => Response)> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    const hit = overrides[path];
    if (hit) return typeof hit === "function" ? hit() : hit.clone();
    const key = Object.keys(responses).find((k) => k === path) ?? Object.keys(responses).find((k) => path.startsWith(k.split("?")[0]) && k.includes("?") === path.includes("?"));
    if (key) return json(responses[key]);
    if (path.startsWith("/api/dashboard")) return json(responses["/api/dashboard"]);
    if (path.startsWith("/api/patients?")) return json(responses["/api/patients?q=&limit=12"]);
    return json({ error: { code: "not_found", message: `no fixture for ${path}` } }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <ToastProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("screens render with live data", () => {
  const pages: [string, RegExp][] = [
    ["/dashboard", /Clinical Operations/],
    ["/consultations", /Clinical Review & CDS/],
    ["/consultations/new", /New consultation/],
    [`/consultations/${RID}`, /Clinical decision support/],
    ["/coding", /Coding queue/],
    [`/coding/${RID}`, /Medical coding/],
    ["/billing", /Charge review/],
    [`/billing/${RID}`, /Billing guardrail/],
    ["/oversight", /Oversight dashboard/],
    ["/users", /User access/],
    ["/knowledge", /Harrison's Principles/],
  ];
  for (const [path, heading] of pages) {
    it(`renders ${path}`, async () => {
      mockServer();
      renderAt(path);
      expect(await screen.findByText(heading, {}, { timeout: 4000 })).toBeTruthy();
      expect(screen.queryByText(/Could not load this page/)).toBeNull();
    });
  }

  it("shows sign-in when the session is signed out, with the server's notice", async () => {
    mockServer({ "/api/auth/session": json({ state: "signed_out", user: null, notice: "You were signed out after 30 minutes of inactivity." }) });
    renderAt("/dashboard");
    expect(await screen.findByText(/30 minutes of inactivity/)).toBeTruthy();
  });

  it("shows first-admin setup on an empty system", async () => {
    mockServer({ "/api/auth/session": json({ state: "bootstrap", user: null }) });
    renderAt("/");
    expect(await screen.findByText(/Create the first administrator/)).toBeTruthy();
  });

  it("explains a server that is down instead of a blank page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    renderAt("/");
    expect((await screen.findAllByText(/Cannot reach the NuuCare server/)).length).toBeGreaterThan(0);
  });

  it("a doctor's sidebar has no coding or billing", async () => {
    const session = structuredClone(responses["/api/auth/session"]) as { user: { pages: string[]; role: string } };
    session.user.pages = ["dashboard", "consultations", "review", "knowledge"];
    session.user.role = "Clinical Reviewer";
    mockServer({ "/api/auth/session": json(session) });
    renderAt("/coding");
    expect(await screen.findByText(/You don't have access to this page/)).toBeTruthy();
    expect(screen.queryByText("Coding & HCC")).toBeNull();
  });
});

describe("api client contract", () => {
  it("turns the error envelope into ApiError with code and fields", async () => {
    mockServer({ "/api/x": json({ error: { code: "validation_failed", message: "dob: bad", fields: { dob: "bad" } } }, 422) });
    const error = await api.get("/api/x").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("validation_failed");
    expect(error.fields.dob).toBe("bad");
  });

  it("flags stale-record conflicts", async () => {
    mockServer({ "/api/x": json({ error: { code: "stale_record", message: "changed" } }, 409) });
    const error = await api.post("/api/x", {}).catch((e) => e);
    expect(error.isConflict).toBe(true);
  });

  it("sends the CSRF header on writes only", async () => {
    const fetchMock = mockServer({ "/api/x": json({ ok: true }) });
    await api.post("/api/x", {});
    await api.get("/api/x");
    const headers = fetchMock.mock.calls.map((call) => (call[1] as RequestInit).headers as Record<string, string>);
    expect(headers[0]["X-NuuCare-Client"]).toBe("web");
    expect(headers[1]["X-NuuCare-Client"]).toBeUndefined();
  });

  it("notifies the auth layer on 401 but not for sign-in attempts", async () => {
    const listener = vi.fn();
    const stop = onAuthError(listener);
    mockServer({ "/api/x": json({ error: { code: "session_expired", message: "expired" } }, 401) });
    await api.get("/api/x").catch(() => undefined);
    await request("/api/x", { silentAuth: true }).catch(() => undefined);
    stop();
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("reports an HTML page (wrong server / proxy) as a clear error", async () => {
    mockServer({ "/api/x": new Response("<html>oops</html>", { status: 200, headers: { "content-type": "text/html" } }) });
    const error = await api.get("/api/x").catch((e) => e);
    expect(error.code).toBe("bad_response");
  });

  it("times out instead of hanging forever", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_: unknown, init: RequestInit) => new Promise((_, reject) => {
      init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const pending = request("/api/slow", { timeout: 1000 }).catch((e) => e);
    await act(async () => { vi.advanceTimersByTime(1100); });
    const error = await pending;
    vi.useRealTimers();
    expect(error.code).toBe("timeout");
  });
});

describe("wav encoder", () => {
  it("writes a valid 16-bit mono PCM header the server accepts", async () => {
    const blob = encodeWav(new Float32Array(1600), 16000);
    const bytes = new Uint8Array(await new Promise<ArrayBuffer>((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result as ArrayBuffer); reader.readAsArrayBuffer(blob); }));
    const text = (a: number, b: number) => String.fromCharCode(...bytes.slice(a, b));
    expect(text(0, 4)).toBe("RIFF");
    expect(text(8, 12)).toBe("WAVE");
    expect(bytes.length).toBe(44 + 1600 * 2);
    await waitFor(() => expect(blob.type).toBe("audio/wav"));
  });
});
