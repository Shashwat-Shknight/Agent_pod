import { useEffect, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { PatientDetail, PatientList, PatientRow } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { PatientForm } from "../components/PatientForm";
import { RecordTable } from "../components/RecordTable";
import { useToast } from "../components/toast";
import { Badge, Banner, Card, Empty, ErrorState, Loading, Modal, PageHead } from "../components/ui";

const PAGE = 25;

export default function Patients() {
  const { can } = useAuth();
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [offset, setOffset] = useState(0);
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  // Debounce the search so typing doesn't fire a request per key.
  useEffect(() => {
    const timer = window.setTimeout(() => { setTerm(q.trim()); setOffset(0); }, 250);
    return () => window.clearTimeout(timer);
  }, [q]);

  const query = useQuery({
    queryKey: ["patients", "list", term, offset],
    queryFn: () => api.get<PatientList>(`/api/patients?with_counts=true&limit=${PAGE}&offset=${offset}&q=${encodeURIComponent(term)}`),
    placeholderData: keepPreviousData,
  });
  const data = query.data;

  return (
    <>
      <PageHead eyebrow="Clinical" title="Patients" subtitle="EHR patient register: search, register, update demographics or remove a mistaken entry.">
        {can("Clinical Review", "Edit") && <button className="btn primary" onClick={() => setAdding(true)}>+ Register patient</button>}
      </PageHead>
      <Card>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <input className="input" style={{ maxWidth: 360 }} placeholder="Search by name, patient ID, MRN or date of birth" value={q}
            onChange={(e) => setQ(e.target.value)} aria-label="Search patients" />
          {data && <span className="muted small">{data.total} patient{data.total === 1 ? "" : "s"}</span>}
        </div>
        {query.isLoading && <Loading />}
        {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
        {data && (data.patients.length ? (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Patient</th><th>Patient ID</th><th>MRN</th><th>Date of birth</th><th>Sex</th><th>City</th><th>Consultations</th></tr></thead>
                <tbody>
                  {data.patients.map((p) => (
                    <tr key={p.patient_id} className="clickable" tabIndex={0} onClick={() => setOpenId(p.patient_id)}
                      onKeyDown={(e) => e.key === "Enter" && setOpenId(p.patient_id)}>
                      <td><b>{p.name}</b>{p.patient_id.startsWith("P2") && p.patient_id.length === 7 && <> <Badge tone="blue">Synthetic</Badge></>}</td>
                      <td className="mono">{p.patient_id}</td>
                      <td className="mono">{p.mrn || "—"}</td>
                      <td>{p.dob || "—"}</td>
                      <td>{p.sex || "—"}</td>
                      <td>{p.city || "—"}</td>
                      <td>{p.consultations ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pager offset={offset} total={data.total} onChange={setOffset} />
          </>
        ) : <Empty>{term ? `No patient matches “${term}”.` : "No patients yet."}</Empty>)}
      </Card>
      {adding && <PatientForm mode="create" onClose={() => setAdding(false)} onSaved={(p: PatientRow) => { setAdding(false); setOpenId(p.patient_id); }} />}
      {openId && <PatientPanel patientId={openId} onClose={() => setOpenId(null)} />}
    </>
  );
}

function Pager({ offset, total, onChange }: { offset: number; total: number; onChange: (offset: number) => void }) {
  if (total <= PAGE) return null;
  return (
    <div className="row" style={{ justifyContent: "flex-end", marginTop: 10 }}>
      <span className="muted small">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
      <button className="btn sm" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - PAGE))}>Previous</button>
      <button className="btn sm" disabled={offset + PAGE >= total} onClick={() => onChange(offset + PAGE)}>Next</button>
    </div>
  );
}

function PatientPanel({ patientId, onClose }: { patientId: string; onClose: () => void }) {
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const detail = useQuery({ queryKey: ["patient", patientId], queryFn: () => api.get<PatientDetail>(`/api/patients/${patientId}`) });
  const remove = useMutation({
    mutationFn: (etag: string) => api.del<{ deleted: string }>(`/api/patients/${patientId}?etag=${encodeURIComponent(etag)}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["patients"] });
      toast(`Patient ${patientId} deleted`, "ok");
      onClose();
    },
  });

  if (editing && detail.data) {
    return <PatientForm mode="edit" detail={detail.data} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); toast("Patient updated", "ok"); }} />;
  }
  const d = detail.data;
  return (
    <Modal title={d ? `${d.patient.name} · ${d.patient.patient_id}` : "Patient"} onClose={onClose}>
      {detail.isLoading && <Loading />}
      {detail.isError && <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />}
      {d && (
        <>
          <dl className="grid grid-2 small">
            <div><dt className="muted">MRN</dt><dd className="mono">{d.patient.mrn || "—"}</dd></div>
            <div><dt className="muted">Date of birth · sex</dt><dd>{d.patient.dob || "—"} · {d.patient.sex || "—"}</dd></div>
            <div><dt className="muted">Mobile</dt><dd>{d.patient.mobile || "—"}</dd></div>
            <div><dt className="muted">Email</dt><dd>{d.patient.email || "—"}</dd></div>
            <div><dt className="muted">Address</dt><dd>{[d.patient.street_address, d.patient.city, d.patient.state, d.patient.zip].filter(Boolean).join(", ") || "—"}</dd></div>
            <div><dt className="muted">Chart rows in the EHR</dt><dd>{d.chart_rows}</dd></div>
          </dl>
          <h3 className="mt">Consultations ({d.consultations.length})</h3>
          <RecordTable rows={d.consultations} showStage onOpen={(row) => navigate(`/consultations/${row.id}`)} empty="No consultations for this patient." />
          {remove.isError && <Banner tone="error" title={errorMessage(remove.error)} />}
          {confirming ? (
            <Banner tone="warn" title={`Delete ${d.patient.name}?`} action={
              <span className="row">
                <button className="btn sm" onClick={() => setConfirming(false)}>Keep</button>
                <button className="btn sm danger" disabled={remove.isPending} onClick={() => remove.mutate(d.patient.etag)}>{remove.isPending ? "Deleting…" : "Delete patient"}</button>
              </span>}>
              This removes the patient and their chart rows from the EHR. It cannot be undone.
            </Banner>
          ) : (
            <div className="row mt" style={{ justifyContent: "space-between" }}>
              <span className="muted small">{!d.permissions.delete && d.permissions.delete_reason}</span>
              <span className="row">
                {d.permissions.delete && <button className="btn danger" onClick={() => setConfirming(true)}>Delete</button>}
                {d.permissions.edit && <button className="btn primary" onClick={() => setEditing(true)}>Edit details</button>}
              </span>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
