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

/** The rejection of a call that must fail, typed as ApiError. */
const fail = (call: Promise<unknown>) => call.then(() => { throw new Error("expected the call to fail"); }, (e: unknown) => e as ApiError);

describe("api client contract", () => {
  it("turns the error envelope into ApiError with code and fields", async () => {
    mockServer({ "/api/x": json({ error: { code: "validation_failed", message: "dob: bad", fields: { dob: "bad" } } }, 422) });
    const error = await fail(api.get("/api/x"));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("validation_failed");
    expect(error.fields.dob).toBe("bad");
  });

  it("flags stale-record conflicts", async () => {
    mockServer({ "/api/x": json({ error: { code: "stale_record", message: "changed" } }, 409) });
    const error = await fail(api.post("/api/x", {}));
    expect(error.isConflict).toBe(true);
  });

  it("sends the CSRF header on writes only", async () => {
    const fetchMock = mockServer({ "/api/x": json({ ok: true }) });
    await api.post("/api/x", {});
    await api.get("/api/x");
    const headers = (fetchMock.mock.calls as unknown as [string, RequestInit][]).map((call) => call[1].headers as Record<string, string>);
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
    const error = await fail(api.get("/api/x"));
    expect(error.code).toBe("bad_response");
  });

  it("times out instead of hanging forever", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_: unknown, init: RequestInit) => new Promise((_, reject) => {
      init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const pending = fail(request("/api/slow", { timeout: 1000 }));
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

describe("create / read / update / delete", () => {
  type Session = { user: { pages: string[] } };
  const session = () => {
    const value = structuredClone(responses["/api/auth/session"]) as Session;
    value.user.pages = [...value.user.pages, "patients"];
    return json(value);
  };
  const LIST = "/api/patients?with_counts=true&limit=25&offset=0&q=";
  const list = { total: 1, offset: 0, limit: 25, patients: [{ patient_id: "P200001", name: "Ava Chen", mrn: "MRN200001", dob: "1970-03-04", sex: "Female", city: "Austin", consultations: 0 }] };
  const detail = {
    patient: { patient_id: "P200001", mrn: "MRN200001", name: "Ava Chen", status: "Active", etag: "abc123", first_name: "Ava", last_name: "Chen",
      dob: "1970-03-04", sex: "Female", mobile: "", email: "", street_address: "", city: "Austin", state: "TX", zip: "", preferred_language: "" },
    consultations: [], chart_rows: 0, permissions: { edit: true, delete: true, delete_reason: "" },
  };
  const calls = (mock: ReturnType<typeof mockServer>, method: string) =>
    (mock.mock.calls as unknown as [string, RequestInit][]).filter(([, init]) => init?.method === method);

  it("lists patients and edits one, sending the etag and only the changed fields", async () => {
    const mock = mockServer({ "/api/auth/session": session(), [LIST]: json(list), "/api/patients/P200001": () => json(detail) });
    renderAt("/registry");
    (await screen.findByText("Ava Chen")).click();
    (await screen.findByText("Edit details")).click();
    const city = await screen.findByLabelText("City");
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(city, { target: { value: "Denver" } });
    screen.getByText("Save changes").click();
    await waitFor(() => expect(calls(mock, "PUT").length).toBe(1));
    const [path, init] = calls(mock, "PUT")[0];
    expect(path).toBe("/api/patients/P200001");
    expect(JSON.parse(String(init.body))).toEqual({ etag: "abc123", city: "Denver" });
  });

  it("deletes a patient only after confirmation, naming the etag", async () => {
    const mock = mockServer({ "/api/auth/session": session(), [LIST]: json(list), "/api/patients/P200001": () => json(detail),
      "/api/patients/P200001?etag=abc123": () => json({ deleted: "P200001" }) });
    renderAt("/registry");
    (await screen.findByText("Ava Chen")).click();
    (await screen.findByText("Delete")).click();
    expect(calls(mock, "DELETE").length).toBe(0);
    (await screen.findByText("Delete patient")).click();
    await waitFor(() => expect(calls(mock, "DELETE")[0]?.[0]).toBe("/api/patients/P200001?etag=abc123"));
  });

  it("offers 'register anyway' after a duplicate warning", async () => {
    let posts = 0;
    const mock = mockServer({ "/api/auth/session": session(), [LIST]: json(list), "/api/patients/P300000": () => json({ ...detail, patient: { ...detail.patient, patient_id: "P300000" } }),
      "/api/patients": () => (++posts === 1
        ? json({ error: { code: "possible_duplicate", message: "Same name and DOB: P200001", fields: { patient_ids: "P200001" } } }, 409)
        : json({ patient_id: "P300000", mrn: "MRN300000" })) });
    renderAt("/registry");
    (await screen.findByText("+ Register patient")).click();
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(await screen.findByLabelText("First name"), { target: { value: "Ava" } });
    fireEvent.change(screen.getByLabelText("Last name"), { target: { value: "Chen" } });
    fireEvent.change(screen.getByLabelText("Date of birth"), { target: { value: "1970-03-04" } });
    screen.getByText("Register patient").click();
    expect(await screen.findByText("Possible duplicate patient")).toBeTruthy();
    (await screen.findByLabelText(/register anyway/)).click();
    screen.getByText("Register patient").click();
    await waitFor(() => expect(calls(mock, "POST").length).toBe(2));
    expect(JSON.parse(String(calls(mock, "POST")[1][1].body)).allow_duplicate).toBe(true);
  });

  it("deletes a draft consultation with its version; signed ones have no delete button", async () => {
    const consultations = structuredClone(responses["/api/consultations"]) as { consultations: Record<string, unknown>[] };
    const draft = { ...consultations.consultations[0], id: "PT-DRAFT1", version: 3, status: "Pending", stage: "Clinical Review", name: "Draft Person" };
    consultations.consultations.push(draft);
    const mock = mockServer({ "/api/consultations": json(consultations), "/api/consultations/PT-DRAFT1?version=3": () => json({ deleted: "PT-DRAFT1", warnings: [] }) });
    renderAt("/consultations");
    const buttons = await screen.findAllByRole("button", { name: /Delete consultation/ });
    expect(buttons.length).toBe(1);  // only the draft
    buttons[0].click();
    (await screen.findByText("Delete draft")).click();
    await waitFor(() => expect(calls(mock, "DELETE")[0]?.[0]).toBe("/api/consultations/PT-DRAFT1?version=3"));
  });

  it("deletes a user after confirmation", async () => {
    const users = structuredClone(responses["/api/admin/users"]) as { users: { username: string }[] };
    users.users.push({ ...users.users[0], username: "syn.coder", name: "Priya Nair", role: "Coding Specialist" } as never);
    const mock = mockServer({ "/api/admin/users": json(users), "/api/admin/users/syn.coder": () => json({ users: users.users.slice(0, -1), released: 0 }) });
    renderAt("/users");
    await screen.findByText("Priya Nair");
    const deletes = screen.getAllByRole("button", { name: "Delete" });
    deletes[deletes.length - 1].click();
    (await screen.findByText("Delete account")).click();
    await waitFor(() => expect(calls(mock, "DELETE")[0]?.[0]).toBe("/api/admin/users/syn.coder"));
  });
});

describe("PHI masking in the UI", () => {
  it("tells a role with masked data what is hidden, and locks masked fields when editing", async () => {
    const value = structuredClone(responses["/api/auth/session"]) as { user: { pages: string[]; role: string; permissions: Record<string, Record<string, boolean>> } };
    value.user.pages = [...value.user.pages, "patients"];
    value.user.role = "Coding Specialist";
    value.user.permissions["PHI: Identity"] = { View: true };
    value.user.permissions["PHI: Contact details"] = { View: false };
    value.user.permissions["PHI: Clinical narrative"] = { View: true };
    const LIST = "/api/patients?with_counts=true&limit=25&offset=0&q=";
    const detail = {
      patient: { patient_id: "P200001", mrn: "MRN200001", name: "Ava Chen", status: "Active", etag: "e1", first_name: "Ava", last_name: "Chen",
        dob: "1970-03-04", sex: "Female", mobile: "(***) ***-1002", email: "a***@***.test", street_address: "***", city: "***", state: "TX", zip: "787**", preferred_language: "" },
      consultations: [], chart_rows: 0, permissions: { edit: true, delete: false, delete_reason: "", masked: ["contact"] },
    };
    mockServer({ "/api/auth/session": json(value), [LIST]: json({ total: 1, offset: 0, limit: 25, patients: [{ patient_id: "P200001", name: "Ava Chen", mrn: "MRN200001", dob: "1970-03-04", sex: "Female" }] }),
      "/api/patients/P200001": () => json(detail) });
    renderAt("/registry");
    expect(await screen.findByText("Masked view")).toBeTruthy();
    expect(screen.getByText(/phone, email, address/)).toBeTruthy();
    (await screen.findByText("Ava Chen")).click();
    (await screen.findByText("Edit details")).click();
    expect((await screen.findByLabelText("Mobile") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("First name") as HTMLInputElement).disabled).toBe(false);
  });
});
