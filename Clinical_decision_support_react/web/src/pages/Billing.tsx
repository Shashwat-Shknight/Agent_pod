import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError, download, errorMessage } from "../api/client";
import type { BillingView, RecordSummary } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { RecordTable } from "../components/RecordTable";
import { useToast } from "../components/toast";
import { Badge, Banner, Card, Checklist, Empty, ErrorState, Kpi, Loading, Modal, money, PageHead } from "../components/ui";

const GUARD_TONE = { BILLABLE: "green", BLOCKED: "red", UNVERIFIED: "amber", EXCLUDED: "gray" } as const;

export default function Billing() {
  const { id } = useParams();
  return id ? <Workspace id={id} /> : <Queue />;
}

function Queue() {
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ["billing-queue"], queryFn: () => api.get<{ queue: RecordSummary[] }>("/api/billing/queue") });
  const rows = query.data?.queue ?? [];
  return (
    <>
      <PageHead eyebrow="Billing & RCM" title="Charge review" subtitle="Only services performed AND documented reach the bill" />
      {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
      {query.isLoading && <Loading />}
      {query.data && (
        <>
          <Card title="Ready to bill" subtitle="Signed encounters not yet sent to RCM">
            <RecordTable rows={rows.filter((r) => !r.bill_generated_at)} onOpen={(r) => navigate(`/billing/${r.id}`)} empty="Nothing waiting to be billed." />
          </Card>
          <Card title="Sent to RCM">
            <RecordTable rows={rows.filter((r) => r.bill_generated_at)} onOpen={(r) => navigate(`/billing/${r.id}`)} empty="No bills sent yet." />
          </Card>
        </>
      )}
    </>
  );
}

function Workspace({ id }: { id: string }) {
  const navigate = useNavigate();
  const toast = useToast();
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const key = ["billing", id];
  const query = useQuery({ queryKey: key, queryFn: () => api.get<BillingView>(`/api/billing/${id}`) });
  const [attested, setAttested] = useState(false);
  const [overrides, setOverrides] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState(false);
  const [stale, setStale] = useState(false);
  const view = query.data;
  useEffect(() => {
    if (view) setOverrides(new Set(view.guardrail.filter((g) => g.overridden).map((g) => g.code)));
  }, [view]);

  const onError = (error: unknown) => (error instanceof ApiError && error.isConflict ? setStale(true) : toast(errorMessage(error), "error"));
  const saveOverrides = useMutation({
    mutationFn: () => api.post<BillingView>(`/api/billing/${id}/overrides`, { version: view!.summary.version, codes: [...overrides] }),
    onSuccess: (fresh) => { queryClient.setQueryData(key, fresh); toast("Overrides saved", "ok"); },
    onError,
  });
  const send = useMutation({
    mutationFn: () => api.post<BillingView>(`/api/billing/${id}/send`, { version: view!.summary.version, attested: true }),
    onSuccess: (fresh) => {
      queryClient.setQueryData(key, fresh);
      void queryClient.invalidateQueries({ queryKey: ["billing-queue"] });
      void queryClient.invalidateQueries({ queryKey: ["coding", id] });
      toast(fresh.sent?.exceptions.length ? `Sent to RCM with ${fresh.sent.exceptions.length} exception(s)` : "Sent to RCM", "ok");
      setAttested(false);
    },
    onError,
  });
  const pdf = useMutation({ mutationFn: () => download(`/api/billing/${id}/pdf`, `bill_${id}.pdf`), onError });

  if (query.isLoading) return <Loading label="Building the charge review…" />;
  if (query.isError || !view) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const held = view.guardrail.filter((g) => g.overridable);
  const savedOverrides = new Set(view.guardrail.filter((g) => g.overridden).map((g) => g.code));
  const overridesDirty = held.some((g) => overrides.has(g.code) !== savedOverrides.has(g.code));
  const priced = view.bill_rows.filter((r) => r["Pricing Status"] === "Priced");

  return (
    <>
      <PageHead eyebrow="Billing & RCM" title={`Charge review · ${view.summary.name}`} subtitle={`${view.summary.id} · ${view.encounter_type} · ${view.summary.date}`}>
        <button className="btn" disabled={!view.bill_rows.length || pdf.isPending} onClick={() => pdf.mutate()}>Download draft (PDF)</button>
        {can("Codes") && <button className="btn" onClick={() => navigate(`/coding/${id}`)}>← Coding</button>}
        <button className="btn" onClick={() => navigate("/billing")}>Queue</button>
      </PageHead>
      {stale && <Banner tone="warn" title="This encounter changed since you opened it" action={<button className="btn sm" onClick={() => { setStale(false); void query.refetch(); }}>Reload</button>}>Nothing was overwritten.</Banner>}
      {view.sent && (
        <Banner tone="ok" title={`Sent to RCM on ${view.sent.at} by ${view.sent.by}`}>
          {view.sent.exceptions.length ? `${view.sent.exceptions.length} exception(s) travelled with the bill for the RCM work queue.` : "No exceptions."}
        </Banner>
      )}
      <div className="grid grid-3" style={{ marginBottom: 14 }}>
        <Kpi tone="mint" label="Gross charges on the bill" value={money(view.total)} hint={`${priced.length} priced line(s)`} />
        <Kpi tone="rose" label="Held by the guardrail" value={money(view.held_value)} hint={`${held.length} line(s) need evidence or override`} />
        <Kpi tone="butter" label="Exceptions for RCM" value={view.exceptions.length} hint={view.unpriced.length ? `${view.unpriced.length} unpriced code(s)` : "Validation + pricing"} />
      </div>
      <Card title="Billing guardrail" subtitle="Each coded service, classified from performed-status evidence">
        {view.guardrail.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Code</th><th>Service</th><th>Order status</th><th>Evidence</th><th>Guardrail</th><th>Reason</th>{view.can_override && !view.sent && <th>Confirm performed</th>}</tr></thead>
              <tbody>
                {view.guardrail.map((g) => (
                  <tr key={g.code}>
                    <td className="mono"><b>{g.code}</b></td><td>{g.service}</td><td>{g.order}</td>
                    <td className="small">{g.evidence}<div className="muted">{g.documentation}</div></td>
                    <td><Badge tone={GUARD_TONE[g.guardrail]}>{g.guardrail}</Badge></td>
                    <td className="small">{g.reason}</td>
                    {view.can_override && !view.sent && (
                      <td>{g.overridable && <input type="checkbox" aria-label={`Confirm ${g.code} performed`} checked={overrides.has(g.code)}
                        onChange={(e) => setOverrides((s) => { const n = new Set(s); e.target.checked ? n.add(g.code) : n.delete(g.code); return n; })} />}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No CPT/HCPCS service is coded yet. Code the encounter first.</Empty>}
        {overridesDirty && (
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
            <span className="small muted">A coder override releases a held line and is recorded in the audit trail.</span>
            <button className="btn primary sm" disabled={saveOverrides.isPending} onClick={() => saveOverrides.mutate()}>Save overrides</button>
          </div>
        )}
      </Card>
      <div className="split" style={{ marginTop: 14 }}>
        <Card title="Draft bill" subtitle="Chargemaster gross charges (demo)">
          {view.bill_rows.length ? (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Code</th><th>Service</th><th>Department</th><th>Qty</th><th>Unit</th><th>Gross</th></tr></thead>
                <tbody>
                  {view.bill_rows.map((r) => (
                    <tr key={r["CPT/HCPCS Code"]}>
                      <td className="mono">{r["CPT/HCPCS Code"]}</td><td>{r["Service Description"]}</td><td>{r.Department}</td><td>{r.Quantity}</td>
                      <td>{r["Unit Charge"]}</td><td>{r["Pricing Status"] === "Priced" ? <b>{r["Gross Charge"]}</b> : <Badge tone="amber">Unpriced</Badge>}</td>
                    </tr>
                  ))}
                  <tr><td colSpan={5} style={{ textAlign: "right" }}><b>Total</b></td><td><b>{money(view.total)}</b></td></tr>
                </tbody>
              </table>
            </div>
          ) : <Empty>No billable lines.</Empty>}
          <p className="small muted">{view.notice}</p>
        </Card>
        <div className="stack">
          {view.post_checks && (
            <Card title="Post-coding validation" trailing={<Badge tone={{ pass: "green", warn: "amber", fail: "red" }[view.post_checks.status]}>{view.post_checks.score}/100</Badge>}>
              <Checklist items={view.post_checks.checks.filter((c) => c.status !== "pass").map((c) => ({ label: c.name, status: c.status, detail: c.detail }))} empty="All checks pass." />
            </Card>
          )}
          {!view.sent && (
            <Card title="Release to RCM">
              {view.blockers.map((b) => <Banner key={b} tone="warn" title={b} />)}
              {view.exceptions.length > 0 && (
                <Banner tone="info" title={`${view.exceptions.length} exception(s) will travel with this bill`}>
                  {view.exceptions.map((e) => `${e.check}: ${e.detail}`).join(" · ")}
                </Banner>
              )}
              <label className="checkbox small">
                <input type="checkbox" checked={attested} disabled={!view.can_send} onChange={(e) => setAttested(e.target.checked)} />
                <span>I attest these services were performed and documented{view.exceptions.length ? ", and I acknowledge the listed exceptions" : ""}.</span>
              </label>
              <button className="btn primary block" style={{ marginTop: 10 }} disabled={!view.can_send || !attested || send.isPending} onClick={() => setConfirm(true)}>
                {send.isPending ? "Sending…" : `Send ${money(view.total)} to RCM`}
              </button>
            </Card>
          )}
        </div>
      </div>
      {confirm && (
        <Modal title="Send this bill to RCM?" onClose={() => setConfirm(false)}>
          <p className="small">{priced.length} line(s), {money(view.total)}. Coding is locked once the bill is sent.</p>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn" onClick={() => setConfirm(false)}>Cancel</button>
            <button className="btn primary" onClick={() => { setConfirm(false); send.mutate(); }}>Send to RCM</button>
          </div>
        </Modal>
      )}
    </>
  );
}
