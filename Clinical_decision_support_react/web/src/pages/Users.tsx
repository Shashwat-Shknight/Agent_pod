import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorMessage } from "../api/client";
import type { Action, RoleMatrix, UserRow } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { useToast } from "../components/toast";
import { Badge, Banner, Card, ErrorState, Loading, Modal, PageHead, Tabs } from "../components/ui";

type Tab = "users" | "roles" | "audit";
interface UsersResponse { users: UserRow[]; roles: string[]; clinical_roles: string[] }

export default function Users() {
  const [tab, setTab] = useState<Tab>("users");
  return (
    <>
      <PageHead eyebrow="Administration" title="User access" subtitle="Accounts, role permissions and the access audit trail" />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "users", label: "Users" }, { id: "roles", label: "Role permissions" }, { id: "audit", label: "Audit trail" }]} />
      {tab === "users" && <UsersTab />}
      {tab === "roles" && <RolesTab />}
      {tab === "audit" && <AuditTab />}
    </>
  );
}

function UsersTab() {
  const { user: me, can } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["users"], queryFn: () => api.get<UsersResponse>("/api/admin/users") });
  const [adding, setAdding] = useState(false);
  const [resetting, setResetting] = useState<UserRow | null>(null);
  const editable = can("Super Admin", "Edit");
  const update = useMutation({
    mutationFn: ({ username, changes }: { username: string; changes: Partial<UserRow> }) => api.patch<UsersResponse>(`/api/admin/users/${username}`, changes),
    onSuccess: (data) => { queryClient.setQueryData(["users"], (old: UsersResponse | undefined) => old && { ...old, users: data.users }); toast("Saved", "ok"); },
    onError: (error) => toast(errorMessage(error), "error"),
  });
  const unlock = useMutation({
    mutationFn: (username: string) => api.post<UsersResponse>(`/api/admin/users/${username}/unlock`),
    onSuccess: (data) => queryClient.setQueryData(["users"], (old: UsersResponse | undefined) => old && { ...old, users: data.users }),
    onError: (error) => toast(errorMessage(error), "error"),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError || !query.data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return (
    <Card title="Users" subtitle="New users get a temporary password and must change it at first sign-in"
      trailing={editable ? <button className="btn primary sm" onClick={() => setAdding(true)}>+ Add user</button> : undefined}>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last sign-in</th>{editable && <th />}</tr></thead>
          <tbody>
            {query.data.users.map((u) => (
              <tr key={u.username}>
                <td><b>{u.name}</b><div className="small muted">@{u.username}{u.email ? ` · ${u.email}` : ""}</div></td>
                <td>
                  {editable && u.username !== me?.username ? (
                    <select className="input" value={u.role} disabled={update.isPending} onChange={(e) => update.mutate({ username: u.username, changes: { role: e.target.value } })}>
                      {query.data.roles.map((r) => <option key={r}>{r}</option>)}
                    </select>
                  ) : u.role}
                </td>
                <td className="row">
                  <Badge tone={u.active ? "green" : "gray"}>{u.active ? "Active" : "Inactive"}</Badge>
                  {u.locked && <Badge tone="red">Locked</Badge>}
                  {u.must_change_password && <Badge tone="amber">Temp password</Badge>}
                </td>
                <td className="small">{u.last_login ?? "Never"}</td>
                {editable && (
                  <td className="row">
                    {u.locked && <button className="btn sm" onClick={() => unlock.mutate(u.username)}>Unlock</button>}
                    <button className="btn sm" onClick={() => setResetting(u)}>Reset password</button>
                    {u.username !== me?.username && (
                      <button className="btn sm" disabled={update.isPending} onClick={() => update.mutate({ username: u.username, changes: { active: !u.active } })}>
                        {u.active ? "Deactivate" : "Activate"}
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {adding && <AddUser roles={query.data.roles} onClose={() => setAdding(false)} />}
      {resetting && <ResetPassword user={resetting} onClose={() => setResetting(null)} />}
    </Card>
  );
}

function AddUser({ roles, onClose }: { roles: string[]; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ username: "", name: "", email: "", role: "Clinical Reviewer", password: "" });
  const create = useMutation({
    mutationFn: () => api.post<UsersResponse>("/api/admin/users", form),
    onSuccess: (data) => {
      queryClient.setQueryData(["users"], (old: UsersResponse | undefined) => old && { ...old, users: data.users });
      toast(`Created @${form.username.toLowerCase()}. Share the temporary password securely.`, "ok");
      onClose();
    },
  });
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [key]: e.target.value });
  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate(); };
  return (
    <Modal title="Add user" onClose={onClose}>
      {create.isError && <Banner tone="error" title={errorMessage(create.error)} />}
      <form onSubmit={submit}>
        <div className="grid grid-2">
          <div className="field"><label>Username</label><input className="input" value={form.username} onChange={set("username")} autoFocus /></div>
          <div className="field"><label>Full name</label><input className="input" value={form.name} onChange={set("name")} /></div>
          <div className="field"><label>Email</label><input className="input" type="email" value={form.email} onChange={set("email")} /></div>
          <div className="field"><label>Role</label><select className="input" value={form.role} onChange={set("role")}>{roles.map((r) => <option key={r}>{r}</option>)}</select></div>
        </div>
        <div className="field"><label>Temporary password</label><input className="input" type="password" autoComplete="new-password" value={form.password} onChange={set("password")} />
          <span className="hint">At least 8 characters with letters and numbers. The user changes it at first sign-in.</span></div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={create.isPending || !form.username || !form.name || !form.password}>{create.isPending ? "Creating…" : "Create user"}</button>
        </div>
      </form>
    </Modal>
  );
}

function ResetPassword({ user, onClose }: { user: UserRow; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [password, setPassword] = useState("");
  const reset = useMutation({
    mutationFn: () => api.post<UsersResponse>(`/api/admin/users/${user.username}/password`, { password }),
    onSuccess: (data) => {
      queryClient.setQueryData(["users"], (old: UsersResponse | undefined) => old && { ...old, users: data.users });
      toast("Password reset. The user is signed out and must change it at next sign-in.", "ok");
      onClose();
    },
  });
  return (
    <Modal title={`Reset password · ${user.name}`} onClose={onClose}>
      {reset.isError && <Banner tone="error" title={errorMessage(reset.error)} />}
      <div className="field"><label>New temporary password</label><input className="input" type="password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} /></div>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={reset.isPending || !password} onClick={() => reset.mutate()}>Reset</button>
      </div>
    </Modal>
  );
}

function RolesTab() {
  const { can } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["roles"], queryFn: () => api.get<RoleMatrix>("/api/admin/roles") });
  const [role, setRole] = useState("");
  const [draft, setDraft] = useState<Record<string, Partial<Record<Action, boolean>>>>({});
  useEffect(() => {
    if (query.data && !role) setRole(query.data.roles.find((r) => r !== "Super Admin") ?? "");
  }, [query.data, role]);
  useEffect(() => { if (query.data && role) setDraft(structuredClone(query.data.matrix[role] ?? {})); }, [query.data, role]);
  const save = useMutation({
    mutationFn: () => api.put<RoleMatrix>(`/api/admin/roles/${encodeURIComponent(role)}`, {
      rows: Object.entries(draft).map(([Module, actions]) => ({ Module, ...actions })),
    }),
    onSuccess: (data) => { queryClient.setQueryData(["roles"], data); toast("Permissions saved", "ok"); },
    onError: (error) => toast(errorMessage(error), "error"),
  });
  if (query.isLoading) return <Loading />;
  if (query.isError || !query.data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const m = query.data;
  const editable = can("Super Admin", "Admin") && role !== "Super Admin";
  const clinician = role === "Clinical Reviewer";
  return (
    <Card title="Role permissions" subtitle="Changes apply at the role's next request; segregation of duties is enforced on the server"
      trailing={<select className="input" style={{ width: "auto" }} value={role} onChange={(e) => setRole(e.target.value)}>{m.roles.map((r) => <option key={r}>{r}</option>)}</select>}>
      {role === "Super Admin" && <Banner tone="info" title="Super Admin always has full access" />}
      {clinician && <Banner tone="info" title="Clinicians can never open Codes">Segregation of duties: clinicians document and sign, coders code.</Banner>}
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Module</th>{m.actions.map((a) => <th key={a}>{a}</th>)}</tr></thead>
          <tbody>
            {m.modules.map((module) => (
              <tr key={module}>
                <td><b>{module}</b></td>
                {m.actions.map((a) => (
                  <td key={a}>
                    <input type="checkbox" aria-label={`${module} ${a}`} checked={Boolean(draft[module]?.[a])}
                      disabled={!editable || (clinician && module === "Codes")}
                      onChange={(e) => setDraft({ ...draft, [module]: { ...draft[module], [a]: e.target.checked } })} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editable && <div className="row" style={{ justifyContent: "flex-end", marginTop: 10 }}><button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>Save permissions</button></div>}
    </Card>
  );
}

function AuditTab() {
  const query = useQuery({ queryKey: ["audit"], queryFn: () => api.get<{ events: { at: string; actor: string; action: string; detail: string }[] }>("/api/admin/audit?limit=300") });
  if (query.isLoading) return <Loading />;
  if (query.isError || !query.data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return (
    <Card title="Access audit trail" subtitle="Latest 300 events">
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>When</th><th>User</th><th>Action</th><th>Detail</th></tr></thead>
          <tbody>{query.data.events.map((e, i) => <tr key={i}><td className="small">{e.at}</td><td>{e.actor}</td><td className="mono">{e.action}</td><td className="small">{e.detail}</td></tr>)}</tbody>
        </table>
      </div>
    </Card>
  );
}
