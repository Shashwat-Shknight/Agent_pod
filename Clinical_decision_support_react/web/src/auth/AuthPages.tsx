import { FormEvent, ReactNode, useState } from "react";
import { request, ApiError, errorMessage } from "../api/client";
import type { SessionResponse } from "../api/types";
import { Banner } from "../components/ui";
import { useAuth } from "./AuthContext";

function AuthShell({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <main className="auth-page">
      <section className="card auth-card">
        <div className="brand">
          <span className="brand-mark">N</span>
          <span>NuuCare<small>Agent Pod · Clinical Decision Platform</small></span>
        </div>
        <h1 style={{ fontSize: "1.3rem", marginBottom: 4 }}>{title}</h1>
        <p className="muted small" style={{ marginTop: 0 }}>{subtitle}</p>
        {children}
      </section>
    </main>
  );
}

function Field({ label, error, hint, children }: { label: string; error?: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && !error && <span className="hint">{hint}</span>}
      {error && <span className="err">{error}</span>}
    </div>
  );
}

const PASSWORD_HINT = "At least 8 characters, with letters and numbers.";

export function SignInPage() {
  const { setSession, notice } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      setSession(await request<SessionResponse>("/api/auth/login", { method: "POST", body: { username: username.trim(), password }, silentAuth: true }));
    } catch (err) {
      setError(errorMessage(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Sign in" subtitle="Use the account your administrator created for you.">
      {notice && !error && <Banner tone="info" title={notice} />}
      {error && <Banner tone="error" title={error} />}
      <form onSubmit={submit} noValidate>
        <Field label="Username">
          <input className="input" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="Password">
          <input className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <button className="btn primary block" disabled={busy || !username.trim() || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </AuthShell>
  );
}

export function BootstrapPage() {
  const { setSession } = useAuth();
  const [form, setForm] = useState({ username: "", name: "", email: "", password: "", confirm: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mismatch = form.confirm.length > 0 && form.confirm !== form.password;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || mismatch) return;
    setBusy(true);
    setError("");
    try {
      const { confirm: _confirm, ...body } = form;
      setSession(await request<SessionResponse>("/api/auth/bootstrap", { method: "POST", body, silentAuth: true }));
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof ApiError && err.code === "already_bootstrapped") window.location.reload();
    } finally {
      setBusy(false);
    }
  }

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: e.target.value });
  return (
    <AuthShell title="Create the first administrator" subtitle="This one-time step creates the Super Admin account. Everyone else is added from User Access.">
      {error && <Banner tone="error" title={error} />}
      <form onSubmit={submit} noValidate>
        <Field label="Username" hint="3-32 characters: letters, numbers, dot, dash, underscore.">
          <input className="input" autoFocus value={form.username} onChange={set("username")} />
        </Field>
        <Field label="Full name"><input className="input" value={form.name} onChange={set("name")} /></Field>
        <Field label="Email (optional)"><input className="input" type="email" value={form.email} onChange={set("email")} /></Field>
        <Field label="Password" hint={PASSWORD_HINT}><input className="input" type="password" autoComplete="new-password" value={form.password} onChange={set("password")} /></Field>
        <Field label="Confirm password" error={mismatch ? "Passwords do not match." : undefined}>
          <input className="input" type="password" autoComplete="new-password" value={form.confirm} onChange={set("confirm")} />
        </Field>
        <button className="btn primary block" disabled={busy || mismatch || !form.username || !form.name || !form.password}>
          {busy ? "Creating…" : "Create administrator"}
        </button>
      </form>
    </AuthShell>
  );
}

export function ChangePasswordPage() {
  const { setSession, logout, user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mismatch = confirm.length > 0 && confirm !== next;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || mismatch) return;
    setBusy(true);
    setError("");
    try {
      setSession(await request<SessionResponse>("/api/auth/password", { method: "POST", body: { current_password: current, new_password: next } }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Choose a new password" subtitle={`Welcome${user ? `, ${user.name}` : ""}. Your password was set by an administrator, so choose your own to continue.`}>
      {error && <Banner tone="error" title={error} />}
      <form onSubmit={submit} noValidate>
        <Field label="Temporary password"><input className="input" type="password" autoComplete="current-password" autoFocus value={current} onChange={(e) => setCurrent(e.target.value)} /></Field>
        <Field label="New password" hint={PASSWORD_HINT}><input className="input" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} /></Field>
        <Field label="Confirm new password" error={mismatch ? "Passwords do not match." : undefined}>
          <input className="input" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        <button className="btn primary block" disabled={busy || mismatch || !current || !next}>{busy ? "Saving…" : "Save and continue"}</button>
        <button type="button" className="btn ghost block" style={{ marginTop: 8 }} onClick={() => void logout()}>Sign out</button>
      </form>
    </AuthShell>
  );
}
