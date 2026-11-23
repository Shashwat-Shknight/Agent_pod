import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError, errorMessage, TIMEOUTS } from "../api/client";
import type { CdsItem, ReviewView } from "../api/types";
import { useToast } from "../components/toast";
import { Badge, Banner, Card, Checklist, Empty, ErrorState, Loading, Modal, PageHead, Spinner, Tabs } from "../components/ui";

type Step = "conversation" | "cds" | "soap";
const PRIORITY_TONE = { HIGH: "red", MED: "amber", ROUTINE: "gray" } as const;
const DECISION_TONE = { accepted: "green", modified: "violet", overridden: "amber" } as const;

export default function Review() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();
  const key = ["consultation", id];
  const query = useQuery({ queryKey: key, queryFn: () => api.get<ReviewView>(`/api/consultations/${id}`) });
  const [step, setStep] = useState<Step>("cds");
  const [stale, setStale] = useState(false);
  const view = query.data;
  const version = view?.summary.version ?? 0;

  /** Every write: send the version we saw, take the server's fresh view, surface warnings and conflicts. */
  const write = useMutation({
    mutationFn: ({ path, body, method = "POST", timeout }: { path: string; body: object; method?: "POST" | "PUT"; timeout?: number }) =>
      method === "PUT" ? api.put<ReviewView>(path, { version, ...body }, timeout) : api.post<ReviewView>(path, { version, ...body }, timeout),
    onSuccess: (fresh) => {
      queryClient.setQueryData(key, fresh);
      fresh.warnings?.forEach((w) => toast(w, "error"));
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      void queryClient.invalidateQueries({ queryKey: ["consultations"] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.isConflict) setStale(true);
      else toast(errorMessage(error), "error");
    },
  });

  if (query.isLoading) return <Loading label="Opening consultation…" />;
  if (query.isError || !view) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;

  const record = view.record;
  const signed = record.status === "Approved";
  const reload = () => { setStale(false); void query.refetch(); };

  return (
    <>
      <PageHead eyebrow={step === "soap" ? "SOAP note & sign" : "Clinical review & CDS"} title={record.name || "Consultation"}
        subtitle={`${record.id} · ${record.date} ${record.time} · ${record.doctor}${record.mrn ? ` · MRN ${record.mrn}` : ""}`}>
        {signed ? <Badge tone="green">Signed · locked</Badge> : view.permissions.edit ? <Badge tone="amber">Draft</Badge> : <Badge tone="gray">Read-only</Badge>}
        <button className="btn" onClick={() => navigate("/consultations")}>All consultations</button>
      </PageHead>
      {stale && (
        <Banner tone="warn" title="Someone else changed this consultation" action={<button className="btn sm" onClick={reload}>Reload latest</button>}>
          Your last change was not saved so nothing was overwritten. Reload, then repeat it.
        </Banner>
      )}
      <div className="stepper" role="tablist">
        {([["conversation", "Conversation"], ["cds", "Decision support"], ["soap", "SOAP note & sign"]] as [Step, string][]).map(([s, label], index) => (
          <button key={s} className={`step ${step === s ? "active" : ""} ${signed && s === "soap" ? "done" : ""}`} onClick={() => setStep(s)}>
            <b>{index + 1}</b>{label}
          </button>
        ))}
      </div>
      {view.chart.context && (
        <div className="context-strip">
          <div><small>Problems</small>{view.chart.context.problems}</div>
          <div><small>Medications</small>{view.chart.context.medications}</div>
          <div><small>Allergies</small>{view.chart.context.allergies}</div>
          <div><small>Latest vitals</small>{view.chart.context.vitals}</div>
          <div><small>Labs</small>{view.chart.context.labs}</div>
        </div>
      )}
      {!view.chart.linked && <Banner tone="warn" title="Not linked to an EHR chart">Medication safety and care-gap checks need the patient's chart.</Banner>}
      {step === "conversation" && <ConversationStep view={view} />}
      {step === "cds" && <CdsStep view={view} busy={write.isPending} onWrite={(path, body, timeout) => write.mutate({ path, body, timeout })} onNext={() => setStep("soap")} />}
      {step === "soap" && <SoapStep view={view} busy={write.isPending} stale={stale}
        onSave={(summary) => write.mutate({ path: `/api/consultations/${id}/note`, body: { summary }, method: "PUT" })}
        onSign={(summary) => write.mutate({ path: `/api/consultations/${id}/sign`, body: { summary } })}
        onBack={() => setStep("cds")} onNext={() => navigate("/consultations/new")} />}
    </>
  );
}

function ConversationStep({ view }: { view: ReviewView }) {
  const entries = view.record.transcript_data ?? [];
  return (
    <div className="split">
      <Card title="Source conversation" subtitle="What the ambient scribe heard">
        {entries.length ? (
          <div className="chat" style={{ maxHeight: 560 }}>
            {entries.map((e, i) => (
              <div key={i} className={`bubble ${e.Speaker === view.record.doctor || /^doctor$/i.test(e.Speaker) ? "doctor" : "patient"}`}>
                <small>{e.Speaker}</small>{e.Text}
              </div>
            ))}
          </div>
        ) : <div className="excerpt">{view.record.transcript || "No conversation stored."}</div>}
      </Card>
      <Card title="Key highlights">
        {view.record.key_highlights?.length ? <ul className="small">{view.record.key_highlights.map((h, i) => <li key={i}>{h}</li>)}</ul> : <p className="muted small">None.</p>}
      </Card>
    </div>
  );
}

function CdsStep({ view, busy, onWrite, onNext }: {
  view: ReviewView; busy: boolean; onWrite: (path: string, body: object, timeout?: number) => void; onNext: () => void;
}) {
  const [tab, setTab] = useState<"recs" | "reasoning" | "evidence" | "safety" | "history">("recs");
  const [insights, setInsights] = useState<string[]>([]);
  const id = view.record.id;
  const editable = view.permissions.edit;
  const passages = view.cds.passages;
  return (
    <div className="split">
      <div className="stack">
        {passages.length ? (
          <Banner tone="ok" title={`Evidence quality guard active · ${passages.length} Harrison's passage(s) attached`}>
            Recommendations were generated only from the retrieved knowledge-base passages listed under Evidence.
          </Banner>
        ) : (
          <Banner tone="warn" title="No guideline passages retrieved">Treat any recommendation as unreferenced and verify clinically.</Banner>
        )}
        <Card trailing={editable ? <button className="btn ghost sm" disabled={busy} onClick={() => onWrite(`/api/consultations/${id}/cds/regenerate`, {}, TIMEOUTS.ai)}>
          {busy ? <Spinner /> : "↻"} Regenerate CDS</button> : undefined} title="Clinical decision support" subtitle="One recommendation per documented problem">
          <Tabs value={tab} onChange={setTab} tabs={[
            { id: "recs", label: `Recommendations (${view.cds.items.length})` }, { id: "reasoning", label: "Reasoning" },
            { id: "evidence", label: `Evidence (${passages.length})` }, { id: "safety", label: "Safety note" }, { id: "history", label: "History" },
          ]} />
          {tab === "recs" && (view.cds.items.length ? (
            <>
              {view.cds.items.map((item, index) => (
                <Recommendation key={`${index}-${item.title}`} item={item} index={index} view={view} busy={busy} onWrite={onWrite} />
              ))}
              <p className="muted small">Priority comes from the wording (avoid, urgent, allergy → HIGH). Clinician judgment governs.</p>
            </>
          ) : <Empty>No recommendations yet.{editable && " Use “Regenerate CDS”."}</Empty>)}
          {tab === "reasoning" && <div className="soap small">{view.cds.reasoning || "No reasoning provided."}</div>}
          {tab === "evidence" && (passages.length ? passages.map((p, i) => (
            <div key={i} className="excerpt" style={{ marginBottom: 8 }}><small>§ {p.source}</small>{p.text.slice(0, 900)}</div>
          )) : <Empty>No passages were retrieved.</Empty>)}
          {tab === "safety" && <div className="soap small">{view.cds.safety_note || "No safety note provided."}</div>}
          {tab === "history" && (
            <table className="table"><thead><tr><th>When</th><th>Event</th></tr></thead>
              <tbody>{[{ at: `${view.record.date} ${view.record.time}`, action: "Consultation created" }, ...(view.record.review_history ?? [])].map((h, i) => (
                <tr key={i}><td className="small">{h.at}</td><td>{h.action}</td></tr>))}</tbody></table>
          )}
        </Card>
      </div>
      <div className="stack">
        <Card title="Medication safety" subtitle="Rule-based checks on the chart and plan (no AI)">
          <Checklist items={view.medication_safety} empty="Link an EHR patient to run medication checks." />
        </Card>
        <Card title="Care gaps · VBC" subtitle={`${view.chart.coverage || "Commercial"} measure set`}>
          <Checklist items={view.care_gaps} empty="No quality measure applies." />
        </Card>
        <Card title="Coding hand-off" subtitle="Clinicians document, coders code" trailing={<Badge tone={view.handoff === "Queued for coding" ? "violet" : "amber"}>{view.handoff}</Badge>}>
          <p className="small muted" style={{ margin: 0 }}>
            {view.handoff === "Queued for coding" ? "The signed note is in the coding queue. A certified coder assigns ICD-10 / CPT / HCC codes." :
              "Codes are assigned by the coding team after you sign. No codes are shown on this screen."}
          </p>
        </Card>
        {editable && (
          <Card title="Add clinical insight" subtitle="Adds a finding the conversation missed">
            <select className="input" multiple size={6} value={insights} onChange={(e) => setInsights([...e.target.selectedOptions].map((o) => o.value).slice(0, 6))} aria-label="Insights">
              {view.master_insights.map((i) => <option key={i}>{i}</option>)}
            </select>
            <button className="btn sm" style={{ marginTop: 8 }} disabled={busy || !insights.length}
              onClick={() => { onWrite(`/api/consultations/${id}/insights`, { insights }); setInsights([]); }}>Add to note</button>
          </Card>
        )}
        <button className="btn primary" onClick={onNext}>Proceed to SOAP note →</button>
      </div>
    </div>
  );
}

function Recommendation({ item, index, view, busy, onWrite }: {
  item: CdsItem; index: number; view: ReviewView; busy: boolean; onWrite: (path: string, body: object) => void;
}) {
  const [mode, setMode] = useState<"" | "modified" | "overridden">("");
  const [note, setNote] = useState("");
  const decision = view.cds.decisions[String(index)];
  const id = view.record.id;
  const inPlan = (view.record.summary || "").toLowerCase().includes(item.title.toLowerCase());
  const decide = (status: string, text = "") => onWrite(`/api/consultations/${id}/recommendations/${index}/decision`, { status, note: text });
  return (
    <div className={`rec ${item.priority}`}>
      <div className="row">
        <Badge tone={PRIORITY_TONE[item.priority]}>{item.priority}</Badge>
        {decision && <Badge tone={DECISION_TONE[decision.status]}>{decision.status}</Badge>}
        {decision?.note && <span className="small muted">“{decision.note}” · {decision.at}</span>}
        {inPlan && <Badge tone="blue">In plan</Badge>}
      </div>
      <div className="rec-title">{item.title}</div>
      {item.body && <div className="rec-body">{item.body}</div>}
      {view.permissions.edit && (
        <>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn primary sm" disabled={busy} onClick={() => decide("accepted")}>✓ Accept</button>
            <button className="btn sm" disabled={busy} onClick={() => { setMode("modified"); setNote(item.title); }}>Modify</button>
            <button className="btn sm" disabled={busy} onClick={() => { setMode("overridden"); setNote(""); }}>Override…</button>
            <button className="btn ghost sm" disabled={busy || inPlan} onClick={() => onWrite(`/api/consultations/${id}/plan`, { line: item.title })}>
              {inPlan ? "Pushed to plan" : "Push to note ↗"}
            </button>
          </div>
          {mode && (
            <div className="row" style={{ marginTop: 8 }}>
              <input className="input" style={{ flex: 1 }} autoFocus value={note} onChange={(e) => setNote(e.target.value)}
                placeholder={mode === "overridden" ? "Clinical reason for not following this" : "Your wording"} />
              <button className="btn sm primary" disabled={busy || !note.trim()} onClick={() => { decide(mode, note.trim()); setMode(""); }}>Save</button>
              <button className="btn sm" onClick={() => setMode("")}>Cancel</button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function SoapStep({ view, busy, stale, onSave, onSign, onBack, onNext }: {
  view: ReviewView; busy: boolean; stale: boolean; onSave: (s: string) => void; onSign: (s: string) => void; onBack: () => void; onNext: () => void;
}) {
  const server = view.record.summary || "";
  const [text, setText] = useState(server);
  const [base, setBase] = useState(server);
  const [confirming, setConfirming] = useState(false);
  const dirty = text !== base;
  const signed = view.record.status === "Approved";

  // Server text changed (e.g. a CDS item was pushed): adopt it if the user has no unsaved edits.
  useEffect(() => {
    if (server !== base && !dirty) { setText(server); setBase(server); }
    else if (server === text) setBase(server);
  }, [server]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  const grouped = view.insights.reduce<Record<string, string[]>>((acc, i) => ({ ...acc, [i.category]: [...(acc[i.category] ?? []), i.name] }), {});
  return (
    <div className="split">
      <Card title="SOAP note" subtitle={signed ? `Signed by ${view.record.signed_by} on ${view.record.approval_date}` : "Edit before signing. Signed notes are locked."}
        trailing={dirty ? <Badge tone="amber">Unsaved changes</Badge> : undefined}>
        {signed ? <div className="soap">{server}</div> : (
          <textarea className="input mono" style={{ minHeight: 420 }} value={text} disabled={!view.permissions.edit || busy} onChange={(e) => setText(e.target.value)} aria-label="SOAP note" />
        )}
        {signed ? (
          <Banner tone="ok" title="Signed and locked · sent to the coding queue" action={<button className="btn primary sm" onClick={onNext}>Next consultation →</button>}>
            A certified coder assigns ICD-10 / CPT / HCC codes from this note. No further action is needed here.
          </Banner>
        ) : (
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn" onClick={onBack}>← Back to decision support</button>
            <span className="spacer" />
            {view.permissions.edit && <button className="btn" disabled={busy || !dirty || !text.trim() || stale} onClick={() => onSave(text)}>Save draft</button>}
            <button className="btn primary" disabled={busy || !view.permissions.sign || !text.trim() || stale}
              title={view.permissions.sign ? undefined : "Your role cannot sign clinical notes"} onClick={() => setConfirming(true)}>Sign & finalize ✓</button>
          </div>
        )}
      </Card>
      <Card title="Detected insights" subtitle="Clinical signals grouped from the conversation">
        {Object.keys(grouped).length ? Object.entries(grouped).map(([category, names]) => (
          <div key={category} style={{ marginBottom: 10 }}>
            <div className="small muted" style={{ fontWeight: 650, marginBottom: 4 }}>{category}</div>
            <div className="row">{names.map((n) => <Badge key={n} tone={category === "Diagnoses" ? "violet" : category.startsWith("Tests") ? "blue" : "gray"}>{n}</Badge>)}</div>
          </div>
        )) : <p className="muted small">No signals detected.</p>}
      </Card>
      {confirming && (
        <Modal title="Sign and lock this note?" onClose={() => setConfirming(false)}>
          <p className="small">After signing, the note cannot be edited and goes to the coding queue. You will not see codes or billing.</p>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn" onClick={() => setConfirming(false)}>Cancel</button>
            <button className="btn primary" disabled={busy} onClick={() => { setConfirming(false); onSign(text); }}>Sign & finalize</button>
          </div>
        </Modal>
      )}
    </div>
  );
}
