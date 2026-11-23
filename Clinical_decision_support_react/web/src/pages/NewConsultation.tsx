import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, errorMessage, TIMEOUTS } from "../api/client";
import type { PatientContext, PatientRow, ScribeResult } from "../api/types";
import { PatientForm } from "../components/PatientForm";
import { useToast } from "../components/toast";
import { Badge, Banner, Card, ErrorState, PageHead, Spinner } from "../components/ui";
import { MAX_SECONDS, microphoneSupported, startWavCapture, WavCapture } from "../lib/wav";

const ENCOUNTER_TYPES = ["Office Visit", "Outpatient", "Telehealth", "Follow-up", "Inpatient"];
const MAX_FILE_MB = 25;

function useDebounced<T>(value: T, delay = 250) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

function PatientStep({ selected, onSelect }: { selected: PatientRow | null; onSelect: (p: PatientRow) => void }) {
  const [q, setQ] = useState("");
  const [registering, setRegistering] = useState(false);
  const term = useDebounced(q);
  const patients = useQuery({
    queryKey: ["patients", term],
    queryFn: () => api.get<{ patients: PatientRow[] }>(`/api/patients?q=${encodeURIComponent(term)}&limit=12`),
    placeholderData: (previous) => previous,
  });
  return (
    <Card title="1 · Patient" subtitle="Choose the patient from the EHR, or register a new one."
      trailing={<button className="btn sm" onClick={() => setRegistering(true)}>+ Register new patient</button>}>
      <input className="input" placeholder="Search by name, patient ID, MRN or date of birth" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search patients" autoFocus />
      {patients.isError && <ErrorState error={patients.error} onRetry={() => void patients.refetch()} />}
      <div className="table-wrap" style={{ marginTop: 10, maxHeight: 300, overflowY: "auto" }}>
        <table className="table">
          <thead><tr><th>Patient</th><th>Patient ID</th><th>MRN</th><th>DOB</th><th>Sex</th></tr></thead>
          <tbody>
            {(patients.data?.patients ?? []).map((p) => (
              <tr key={p.patient_id} className={`clickable ${selected?.patient_id === p.patient_id ? "selected" : ""}`} onClick={() => onSelect(p)}>
                <td><b>{p.name}</b></td><td className="mono">{p.patient_id}</td><td className="mono">{p.mrn}</td><td>{p.dob}</td><td>{p.sex}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {patients.data && !patients.data.patients.length && <p className="muted small">No patient matches “{term}”. Register them instead.</p>}
      </div>
      {registering && <PatientForm mode="create" onClose={() => setRegistering(false)} onSaved={(p) => { setRegistering(false); onSelect(p); }} />}
    </Card>
  );
}


function PatientContextStrip({ patientId }: { patientId: string }) {
  const context = useQuery({ queryKey: ["patient-context", patientId], queryFn: () => api.get<PatientContext>(`/api/patients/${patientId}/context`) });
  if (context.isLoading) return <Spinner label="Loading chart…" />;
  if (!context.data) return null;
  const c = context.data.context;
  return (
    <div className="context-strip">
      <div><small>Problems</small>{c.problems}</div>
      <div><small>Medications</small>{c.medications}</div>
      <div><small>Allergies</small>{c.allergies}</div>
      <div><small>Latest vitals</small>{c.vitals}</div>
      <div><small>Coverage</small>{context.data.coverage.payer || "—"} {context.data.coverage.plan}</div>
    </div>
  );
}

function MicRecorder({ onDone, disabled }: { onDone: (file: Blob) => void; disabled: boolean }) {
  const capture = useRef<WavCapture | null>(null);
  const [active, setActive] = useState(false);
  const [error, setError] = useState("");
  const [tick, setTick] = useState({ level: 0, seconds: 0 });

  useEffect(() => () => capture.current?.cancel(), []);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(async () => {
      const c = capture.current;
      if (!c) return;
      const seconds = c.seconds();
      setTick({ level: c.level(), seconds });
      if (seconds >= MAX_SECONDS) await stop();
    }, 200);
    return () => window.clearInterval(timer);
  });

  async function start() {
    setError("");
    try {
      capture.current = await startWavCapture();
      setActive(true);
    } catch (err) {
      setError(errorMessage(err));
    }
  }
  async function stop() {
    const c = capture.current;
    capture.current = null;
    setActive(false);
    if (c) {
      if (c.seconds() < 3) { c.cancel(); setError("That was shorter than 3 seconds. Record the whole consultation."); return; }
      onDone(await c.stop());
    }
  }
  if (!microphoneSupported()) return <p className="muted small">Microphone recording is not available in this browser.</p>;
  const mm = Math.floor(tick.seconds / 60), ss = Math.floor(tick.seconds % 60).toString().padStart(2, "0");
  return (
    <div>
      {error && <Banner tone="warn" title={error} />}
      <div className="recorder">
        {active ? (
          <>
            <span className="rec-dot" aria-hidden />
            <b>{mm}:{ss}</b>
            <div className="meter" aria-label="Input level"><span style={{ width: `${Math.min(100, tick.level * 140)}%` }} /></div>
            <button className="btn primary sm" onClick={() => void stop()}>Stop & transcribe</button>
            <button className="btn sm" onClick={() => { capture.current?.cancel(); capture.current = null; setActive(false); }}>Discard</button>
          </>
        ) : (
          <>
            <button className="btn" disabled={disabled} onClick={() => void start()}>● Record consultation</button>
            <span className="muted small">Recorded in your browser as WAV, up to 15 minutes.</span>
          </>
        )}
      </div>
    </div>
  );
}

export default function NewConsultation() {
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [patient, setPatient] = useState<PatientRow | null>(null);
  const [encounterType, setEncounterType] = useState(ENCOUNTER_TYPES[0]);
  const [text, setText] = useState("");
  const [scribe, setScribe] = useState<ScribeResult | null>(null);

  // A finished scribe draft is valuable work: warn before the tab is closed.
  useEffect(() => {
    if (!scribe && !text.trim()) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [scribe, text]);

  const fromText = useMutation({
    mutationFn: () => api.post<ScribeResult>("/api/scribe/text", { transcript: text }, TIMEOUTS.ai),
    onSuccess: setScribe,
  });
  const fromFile = useMutation({
    mutationFn: (file: Blob) => {
      const form = new FormData();
      form.append("file", file, file instanceof File ? file.name : "consultation.wav");
      return api.upload<ScribeResult>("/api/scribe/audio", form);
    },
    onSuccess: setScribe,
  });
  const create = useMutation({
    mutationFn: () => api.post<{ id: string; warnings: string[] }>("/api/consultations",
      { draft_id: scribe!.draft_id, patient_id: patient!.patient_id, encounter_type: encounterType }, TIMEOUTS.ai),
    onSuccess: (data) => {
      data.warnings.forEach((w) => toast(w, "error"));
      void queryClient.invalidateQueries({ queryKey: ["consultations"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      setScribe(null);
      setText("");
      navigate(`/consultations/${data.id}`, { replace: true });
    },
  });
  const busy = fromText.isPending || fromFile.isPending;
  const scribeError = fromText.error ?? fromFile.error;

  function onFile(file: File | undefined) {
    if (!file) return;
    if (file.size > MAX_FILE_MB * 1024 * 1024) { toast(`File is larger than ${MAX_FILE_MB} MB.`, "error"); return; }
    if (!/\.(wav|flac|aiff?|aif)$/i.test(file.name)) { toast("Use a WAV, FLAC or AIFF file.", "error"); return; }
    fromFile.mutate(file);
  }

  return (
    <>
      <PageHead eyebrow="Consultations" title="New consultation" subtitle="Ambient scribe: the conversation becomes a draft SOAP note.">
        <button className="btn" onClick={() => navigate(-1)}>Cancel</button>
      </PageHead>
      <div className="stack">
        <PatientStep selected={patient} onSelect={setPatient} />
        {patient && (
          <Card title="2 · Conversation" subtitle={`${patient.name} · ${patient.patient_id}`}
            trailing={<select className="input" style={{ width: "auto" }} value={encounterType} onChange={(e) => setEncounterType(e.target.value)} aria-label="Encounter type">
              {ENCOUNTER_TYPES.map((t) => <option key={t}>{t}</option>)}
            </select>}>
            <PatientContextStrip patientId={patient.patient_id} />
            <div className="grid grid-2">
              <div>
                <div className="field"><label>Record or upload</label></div>
                <MicRecorder disabled={busy} onDone={(blob) => fromFile.mutate(blob)} />
                <div className="field" style={{ marginTop: 12 }}>
                  <label htmlFor="upload">Upload a recording (WAV, FLAC, AIFF · max 25 MB)</label>
                  <input id="upload" type="file" accept=".wav,.flac,.aif,.aiff" disabled={busy} onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
                </div>
              </div>
              <div>
                <div className="field">
                  <label htmlFor="paste">…or paste the conversation</label>
                  <textarea id="paste" className="input" rows={9} placeholder={"Doctor: What brings you in today?\nPatient: …"} value={text} disabled={busy} onChange={(e) => setText(e.target.value)} />
                  <span className="hint">Lines starting with “Doctor:” and “Patient:” keep the speakers apart.</span>
                </div>
                <button className="btn primary" disabled={busy || text.trim().length < 40} onClick={() => fromText.mutate()}>Generate SOAP draft</button>
              </div>
            </div>
            {busy && <Banner tone="info" title="Ambient scribe is working…">Transcribing and drafting the SOAP note. This usually takes 10–40 seconds.</Banner>}
            {scribeError && <Banner tone="error" title="The scribe could not finish">{errorMessage(scribeError)}</Banner>}
          </Card>
        )}
        {scribe && patient && (
          <Card title="3 · Draft" subtitle={`${scribe.word_count} words captured`} trailing={<Badge tone="amber">Draft · not signed</Badge>}>
            <div className="grid grid-2">
              <div>
                <h3 className="small muted" style={{ marginBottom: 6 }}>CONVERSATION</h3>
                <div className="chat">
                  {scribe.entries.length ? scribe.entries.map((e, i) => (
                    <div key={i} className={`bubble ${/patient/i.test(e.Speaker) ? "patient" : "doctor"}`}><small>{e.Speaker}</small>{e.Text}</div>
                  )) : <div className="excerpt">{scribe.transcript}</div>}
                </div>
              </div>
              <div>
                <h3 className="small muted" style={{ marginBottom: 6 }}>DRAFT SOAP NOTE</h3>
                <div className="excerpt soap">{scribe.soap_note}</div>
                {scribe.key_highlights.length > 0 && <ul className="small">{scribe.key_highlights.map((h, i) => <li key={i}>{h}</li>)}</ul>}
              </div>
            </div>
            {create.isError && <Banner tone="error" title="The consultation was not created">{errorMessage(create.error)}</Banner>}
            <div className="row" style={{ justifyContent: "flex-end", marginTop: 12 }}>
              <button className="btn" disabled={create.isPending} onClick={() => setScribe(null)}>Start over</button>
              <button className="btn primary" disabled={create.isPending} onClick={() => create.mutate()}>
                {create.isPending ? "Running decision support…" : "Proceed to Clinical Review & CDS →"}
              </button>
            </div>
          </Card>
        )}
      </div>
    </>
  );
}
