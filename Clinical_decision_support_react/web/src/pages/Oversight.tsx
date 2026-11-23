import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { OversightRow } from "../api/types";
import { useToast } from "../components/toast";
import { Badge, Banner, Card, Empty, ErrorState, Kpi, Loading, PageHead, STATUS_TONE } from "../components/ui";

const STAGES = ["Ambient Scribe", "Clinical Review", "Coding", "Billed", "EHR record"];
const PAGE = 15;

interface OversightResponse { rows: OversightRow[]; ehr_connected: boolean; users: { username: string; name: string; role: string }[] }

export default function Oversight() {
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["oversight"], queryFn: () => api.get<OversightResponse>("/api/oversight") });
  const [q, setQ] = useState("");
  const [stage, setStage] = useState("All stages");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<OversightRow | null>(null);
  const reassign = useMutation({
    mutationFn: ({ row, username }: { row: OversightRow; username: string }) =>
      api.post(`/api/consultations/${row.record_id}/assign`, { version: row.version, username }),
    onSuccess: () => { toast("Reassigned", "ok"); void queryClient.invalidateQueries({ queryKey: ["oversight"] }); },
    onError: (error) => toast(errorMessage(error), "error"),
  });

  const rows = useMemo(() => {
    const term = q.trim().toLowerCase();
    return (query.data?.rows ?? []).filter((r) =>
      (stage === "All stages" || r.Stage === stage) && (!term || `${r.Patient} ${r.MRN} ${r.Encounter} ${r["Patient ID"]}`.toLowerCase().includes(term)));
  }, [query.data, q, stage]);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  const current = Math.min(page, pages - 1);

  if (query.isLoading) return <Loading />;
  if (query.isError || !query.data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const all = query.data.rows;
  const consults = all.filter((r) => r.record_id);

  return (
    <>
      <PageHead eyebrow="Oversight" title="Oversight dashboard" subtitle="Every patient across documentation, coding and billing" />
      {!query.data.ehr_connected && <Banner tone="warn" title="EHR offline">Showing consultations only.</Banner>}
      <div className="grid grid-5" style={{ marginBottom: 14 }}>
        <Kpi tone="lav" label="Patients" value={all.length} hint={`${consults.length} with a consultation`} />
        <Kpi tone="sky" label="In progress" value={all.filter((r) => r.Priority === "In progress").length} hint="Moving through the workflow" />
        <Kpi tone="rose" label="Needs attention" value={all.filter((r) => r.Priority === "Needs attention").length} hint="Unassigned or rerouted" />
        <Kpi tone="lav" label="In coding" value={all.filter((r) => r.Stage === "Coding").length} hint="Signed, awaiting codes/bill" />
        <Kpi tone="mint" label="Billed" value={all.filter((r) => r.Stage === "Billed").length} hint="Sent to RCM" />
      </div>
      <div className="row" style={{ marginBottom: 12 }}>
        {STAGES.map((s) => <Badge key={s} tone={STATUS_TONE[s] ?? "blue"}>{s} · {all.filter((r) => r.Stage === s).length}</Badge>)}
      </div>
      <div className="split">
        <Card title="All patients" subtitle="Select a row to inspect" trailing={<Badge tone="violet">{rows.length} shown</Badge>}>
          <div className="row" style={{ marginBottom: 10 }}>
            <input className="input" style={{ maxWidth: 300 }} placeholder="Patient / MRN / encounter" value={q} onChange={(e) => { setQ(e.target.value); setPage(0); }} />
            <select className="input" style={{ width: "auto" }} value={stage} onChange={(e) => { setStage(e.target.value); setPage(0); }}>
              <option>All stages</option>{STAGES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </div>
          {rows.length ? (
            <>
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Patient</th><th>MRN</th><th>Workflow</th><th>Stage</th><th>Owner</th><th>Priority</th></tr></thead>
                  <tbody>
                    {rows.slice(current * PAGE, current * PAGE + PAGE).map((r) => (
                      <tr key={`${r["Patient ID"]}-${r.Encounter}`} className={`clickable ${selected === r ? "selected" : ""}`} onClick={() => setSelected(r)}>
                        <td><b>{r.Patient}</b></td><td className="mono">{r.MRN}</td><td>{r.Workflow}</td>
                        <td><Badge tone={STATUS_TONE[r.Stage] ?? "blue"}>{r.Stage}</Badge></td><td>{r.Owner}</td>
                        <td><Badge tone={r.Priority === "Needs attention" ? "red" : r.Priority === "Routine" ? "gray" : "blue"}>{r.Priority}</Badge></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {pages > 1 && (
                <div className="row" style={{ justifyContent: "center", marginTop: 10 }}>
                  <button className="btn sm" disabled={current === 0} onClick={() => setPage(current - 1)}>Previous</button>
                  <span className="small muted">Page {current + 1} of {pages}</span>
                  <button className="btn sm" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>Next</button>
                </div>
              )}
            </>
          ) : <Empty>No patients match.</Empty>}
        </Card>
        <Card title="Selected patient" subtitle="Active workflow">
          {selected ? (
            <div className="stack">
              <div><b>{selected.Patient}</b><div className="small muted">{selected["Patient ID"]} · {selected.Encounter}</div></div>
              <div className="small"><b>Stage:</b> {selected.Stage}<br /><b>Owner:</b> {selected.Owner}<br /><b>Pending:</b> {selected["Pending item"]}<br /><b>Updated:</b> {selected.Updated || "—"}</div>
              {selected.record_id && (
                <>
                  <button className="btn primary" onClick={() => navigate(`/consultations/${selected.record_id}`)}>Open consultation</button>
                  <div className="field">
                    <label>Reassign to</label>
                    <select className="input" defaultValue="" disabled={reassign.isPending}
                      onChange={(e) => e.target.value && reassign.mutate({ row: selected, username: e.target.value })}>
                      <option value="" disabled>Choose a user…</option>
                      {query.data.users.map((u) => <option key={u.username} value={u.username}>{u.name} · {u.role}</option>)}
                    </select>
                  </div>
                </>
              )}
            </div>
          ) : <p className="muted small">Select a patient.</p>}
        </Card>
      </div>
    </>
  );
}
