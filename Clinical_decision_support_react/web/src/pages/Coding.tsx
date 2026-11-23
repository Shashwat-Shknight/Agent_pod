import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError, errorMessage } from "../api/client";
import type { CheckSummary, CodeRow, CodingView, RecordSummary } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { RecordTable } from "../components/RecordTable";
import { useToast } from "../components/toast";
import { Badge, Banner, Card, Checklist, Empty, ErrorState, Kpi, Loading, PageHead } from "../components/ui";

const STATUS_TONE: Record<string, string> = {
  Accepted: "green", Rejected: "gray", "Query sent": "violet", Suggested: "blue", Performed: "green", "E/M check": "violet",
  Planned: "amber", Ordered: "amber", Unverified: "amber", "Symptom only": "gray", Superbill: "blue", "Coder confirmed": "green",
};
const PERFORMED_TEXT: Record<string, string> = {
  performed: "Documented as performed during this encounter",
  ordered_not_performed: "Ordered — no performed evidence yet",
  planned: "Planned — not yet performed", recommended: "Recommended — not yet performed", discussed: "Discussed only",
  superbill: "From the provider's paper superbill", coder_confirmed: "Confirmed performed by a coder",
};

export default function Coding() {
  const { id } = useParams();
  return id ? <Workspace id={id} /> : <Queue />;
}

function Queue() {
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ["coding-queue"], queryFn: () => api.get<{ queue: RecordSummary[] }>("/api/coding/queue") });
  const rows = query.data?.queue ?? [];
  const ready = rows.filter((r) => r.status === "Approved" && !r.bill_generated_at);
  return (
    <>
      <PageHead eyebrow="Coding & HCC" title="Coding queue" subtitle="Signed notes waiting for ICD-10-CM, CPT/HCPCS and HCC capture" />
      {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
      {query.isLoading && <Loading />}
      {query.data && (
        <>
          <Card title="Ready to code" subtitle="Signed and not yet billed">
            <RecordTable rows={ready} onOpen={(r) => navigate(`/coding/${r.id}`)} empty="The coding queue is empty." />
          </Card>
          <Card title="All encounters" subtitle="Drafts can be pre-coded; final coding follows the provider signature">
            <RecordTable rows={rows.filter((r) => !ready.includes(r))} onOpen={(r) => navigate(`/coding/${r.id}`)} empty="Nothing else." />
          </Card>
        </>
      )}
    </>
  );
}

function ValidationCard({ title, subtitle, data }: { title: string; subtitle: string; data: CheckSummary }) {
  const [open, setOpen] = useState(data.status === "fail");
  const tone = { pass: "green", warn: "amber", fail: "red" }[data.status];
  return (
    <Card title={title} subtitle={subtitle} trailing={<Badge tone={tone}>{data.label} · {data.score}/100</Badge>}>
      <div className="row small muted">
        <span>{data.total - data.fails - data.warnings} passed</span>·<span>{data.warnings} warnings</span>·<span>{data.fails} blocking</span>
        <button className="btn ghost sm" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} {data.total} checks</button>
      </div>
      {open && <Checklist items={data.checks.map((c) => ({ label: c.name, status: c.status, detail: c.detail }))} empty="No checks." />}
    </Card>
  );
}

function Workspace({ id }: { id: string }) {
  const navigate = useNavigate();
  const toast = useToast();
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const key = ["coding", id];
  const query = useQuery({ queryKey: key, queryFn: () => api.get<CodingView>(`/api/coding/${id}`) });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [active, setActive] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const view = query.data;
  useEffect(() => { if (view && !active && view.rows[0]) setActive(view.rows[0].key); }, [view, active]);

  const decide = useMutation({
    mutationFn: ({ keys, status, note = "" }: { keys: string[]; status: string; note?: string }) =>
      api.post<CodingView>(`/api/coding/${id}/decisions`, { version: view!.summary.version, keys, status, note }),
    onSuccess: (fresh) => { queryClient.setQueryData(key, fresh); setSelected(new Set()); },
    onError: (error) => (error instanceof ApiError && error.isConflict ? setStale(true) : toast(errorMessage(error), "error")),
  });
  const recompute = useMutation({
    mutationFn: () => api.post<CodingView>(`/api/coding/${id}/recompute`, { version: view!.summary.version }),
    onSuccess: (fresh) => { queryClient.setQueryData(key, fresh); toast("Codes re-matched", "ok"); },
    onError: (error) => (error instanceof ApiError && error.isConflict ? setStale(true) : toast(errorMessage(error), "error")),
  });

  if (query.isLoading) return <Loading label="Matching ICD-10 and CPT/HCPCS codes…" />;
  if (query.isError || !view) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const locked = Boolean(view.summary.bill_generated_at);
  const editable = view.can_edit && !locked;
  const row = view.rows.find((r) => r.key === active) ?? null;
  const toggle = (k: string) => setSelected((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; });

  return (
    <>
      <PageHead eyebrow="Coding & HCC" title={`Medical coding · ${view.summary.name}`}
        subtitle={`${view.summary.id} · ${view.summary.date} · ${view.summary.doctor}`}>
        <Badge tone={view.note.signed ? "green" : "amber"}>{view.note.signed ? "Note signed" : "Note draft"}</Badge>
        <Badge tone="violet">CMS-HCC V28 (demo map)</Badge>
        {editable && <button className="btn" disabled={recompute.isPending} onClick={() => recompute.mutate()}>{recompute.isPending ? "Matching…" : "Re-match codes"}</button>}
        {can("RCM Workspace") && <button className="btn primary" onClick={() => navigate(`/billing/${id}`)}>Billing →</button>}
        <button className="btn" onClick={() => navigate("/coding")}>Queue</button>
      </PageHead>
      {stale && <Banner tone="warn" title="This encounter changed since you opened it" action={<button className="btn sm" onClick={() => { setStale(false); void query.refetch(); }}>Reload</button>}>Nothing was overwritten.</Banner>}
      {locked && <Banner tone="info" title={`Sent to RCM on ${view.summary.bill_generated_at}`}>Coding is locked after the bill is released.</Banner>}
      {!view.note.signed && <Banner tone="warn" title="The note is not signed yet">Final coding should follow the provider's signature.</Banner>}
      <ValidationCard title="Pre-coding validation" subtitle="Documentation readiness: is this encounter codable?" data={view.pre_checks} />
      <div className="split" style={{ marginTop: 14 }}>
        <Card title="Code suggestions" subtitle="Matched against the official code sets; the coder decides"
          trailing={<Badge tone="violet">{view.rows.length} candidates</Badge>}>
          {editable && selected.size > 0 && (
            <div className="row" style={{ marginBottom: 8 }}>
              <span className="small muted">{selected.size} selected</span>
              <button className="btn primary sm" disabled={decide.isPending} onClick={() => decide.mutate({ keys: [...selected], status: "Accepted" })}>Accept selected</button>
              <button className="btn sm" disabled={decide.isPending} onClick={() => decide.mutate({ keys: [...selected], status: "Rejected" })}>Reject selected</button>
            </div>
          )}
          {view.rows.length ? (
            <div className="table-wrap">
              <table className="table">
                <thead><tr>
                  {editable && <th><input type="checkbox" aria-label="Select all" checked={selected.size === view.rows.length}
                    onChange={(e) => setSelected(e.target.checked ? new Set(view.rows.map((r) => r.key)) : new Set())} /></th>}
                  <th>Documentation</th><th>Type</th><th>Code</th><th>HCC</th><th>Match</th><th>Status</th>
                </tr></thead>
                <tbody>
                  {view.rows.map((r) => (
                    <tr key={r.key} className={`clickable ${active === r.key ? "selected" : ""}`} onClick={() => setActive(r.key)}>
                      {editable && <td onClick={(e) => e.stopPropagation()}><input type="checkbox" aria-label={`Select ${r.code}`} checked={selected.has(r.key)} onChange={() => toggle(r.key)} /></td>}
                      <td><b>{r.term}</b><div className="small muted">{r.description}</div></td>
                      <td className="small">{r.type}</td>
                      <td className="mono"><b>{r.code}</b></td>
                      <td>{r.hcc ? <Badge tone="blue">{r.hcc}</Badge> : <span className="muted">—</span>}</td>
                      <td>{r.match != null ? <Badge tone={r.match >= 85 ? "green" : "amber"}>{r.match}%</Badge> : "—"}</td>
                      <td><Badge tone={STATUS_TONE[r.status] ?? "gray"}>{r.status}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <Empty>No codable concepts were found in this note.</Empty>}
        </Card>
        <Evidence row={row} view={view} editable={editable} busy={decide.isPending}
          onDecide={(status, note) => row && decide.mutate({ keys: [row.key], status, note })} />
      </div>
      <Card title="Coding summary" subtitle="Encounter-level coding and risk capture" className="mt">
        <div className="grid grid-5">
          <Kpi tone="lav" label="ICD-10-CM" value={view.totals.icd} />
          <Kpi tone="sky" label="CPT / HCPCS" value={view.totals.cpt} />
          <Kpi tone="mint" label="Accepted" value={view.totals.accepted} hint={`${view.totals.rejected} rejected · ${view.totals.queries} queries`} />
          <Kpi tone="peach" label="HCCs captured" value={view.totals.hccs.length} hint={view.totals.hccs.join(", ") || "None"} />
          <Kpi tone="butter" label="E/M level" value={view.totals.em_code ?? "—"} hint={view.totals.mdm ? `${view.totals.mdm} MDM` : undefined} />
        </div>
      </Card>
      <div className="mt"><ValidationCard title="Post-coding validation" subtitle="Code-set integrity and claim scrub: is this ready to bill?" data={view.post_checks} /></div>
      <Card title="Coding audit log" className="mt">
        {view.audit.length ? (
          <table className="table"><thead><tr><th>When</th><th>Code</th><th>Decision</th><th>By</th><th>Note</th></tr></thead>
            <tbody>{view.audit.map((a, i) => <tr key={i}><td className="small">{a.at}</td><td className="mono">{a.code}</td><td>{a.action}</td><td>{a.by}</td><td className="small">{a.note}</td></tr>)}</tbody>
          </table>
        ) : <p className="muted small">No coding decisions yet.</p>}
      </Card>
    </>
  );
}

function Evidence({ row, view, editable, busy, onDecide }: {
  row: CodeRow | null; view: CodingView; editable: boolean; busy: boolean; onDecide: (status: string, note?: string) => void;
}) {
  if (!row) return <Card title="Evidence & provenance"><p className="muted small">Select a code to see why it was suggested.</p></Card>;
  const labels = { M: "Monitor", E: "Evaluate", A: "Assess", T: "Treat" } as const;
  return (
    <Card title="Evidence & provenance" subtitle={`${row.code} · ${row.type}`}
      trailing={row.match != null ? <Badge tone={row.match >= 85 ? "green" : "amber"}>{row.match}% match</Badge> : undefined}>
      <div className="excerpt"><small>Note excerpt</small>“{row.excerpt || "No supporting sentence was found in the conversation or SOAP note."}”</div>
      {row.meat && (
        <div style={{ marginTop: 10 }}>
          <div className="small muted" style={{ fontWeight: 650 }}>Why this code · MEAT (keyword scan)</div>
          <div className="row" style={{ marginTop: 4 }}>
            {(Object.keys(labels) as (keyof typeof labels)[]).map((k) => <Badge key={k} tone={row.meat![k] ? "green" : "gray"}>{k}: {labels[k]} {row.meat![k] ? "✓" : "—"}</Badge>)}
          </div>
        </div>
      )}
      {row.cdi && (
        <Banner tone="warn" title={`${row.code} is an unspecified code`}
          action={editable ? <button className="btn sm" disabled={busy || row.status === "Query sent"} onClick={() => onDecide("Query sent", "Specificity query")}>Send physician query</button> : undefined}>
          The note may support a more specific code. Do not infer specificity — query the provider.
        </Banner>
      )}
      {row.procedure_status && (
        <p className="small"><b>Performed status:</b> {PERFORMED_TEXT[row.procedure_status] ?? "Could not be verified"}</p>
      )}
      {row.mdm && <p className="small"><b>E/M validation:</b> Level {row.code} implies {row.mdm} MDM — confirm against the signed note.</p>}
      <p className="small muted">Matched “{row.term}” → {row.description} · matcher v{view.matcher_version}</p>
      {editable && (
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn danger" disabled={busy || row.status === "Rejected"} onClick={() => onDecide("Rejected")}>Reject</button>
          <button className="btn primary" disabled={busy || row.status === "Accepted"} onClick={() => onDecide("Accepted")}>Accept</button>
        </div>
      )}
    </Card>
  );
}
