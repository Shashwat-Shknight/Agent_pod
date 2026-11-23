import { FormEvent, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, errorMessage } from "../api/client";
import type { PatientDetail, PatientFields, PatientRow } from "../api/types";
import { Banner, Modal } from "./ui";

const EMPTY: PatientFields = {
  first_name: "", last_name: "", dob: "", sex: "Other", mobile: "", email: "",
  street_address: "", city: "", state: "", zip: "", preferred_language: "",
};

type Props =
  | { mode: "create"; onClose: () => void; onSaved: (patient: PatientRow) => void }
  | { mode: "edit"; detail: PatientDetail; onClose: () => void; onSaved: (detail: PatientDetail) => void };

/** Register or edit an EHR patient. Edits carry the etag the user saw, so a concurrent change is a clear 409. */
export function PatientForm(props: Props) {
  const queryClient = useQueryClient();
  const initial = props.mode === "edit" ? pick(props.detail.patient) : EMPTY;
  const [form, setForm] = useState<PatientFields>(initial);
  const [allowDuplicate, setAllowDuplicate] = useState(false);
  // Fields the viewer only sees masked cannot be edited (the server refuses it too).
  const masked = new Set(props.mode === "edit" ? props.detail.permissions.masked ?? [] : []);
  const locked = (key: keyof PatientFields) =>
    (masked.has("identity") && ["first_name", "last_name", "dob"].includes(key)) ||
    (masked.has("contact") && ["mobile", "email", "street_address", "city", "zip"].includes(key));

  const save = useMutation({
    mutationFn: async () => {
      if (props.mode === "create") {
        return api.post<{ patient_id: string; mrn: string }>("/api/patients", { ...form, allow_duplicate: allowDuplicate });
      }
      // Send only what changed, plus the version (etag) the user was looking at.
      const changes = Object.fromEntries(Object.entries(form).filter(([key, value]) => value !== initial[key as keyof PatientFields]));
      return api.put<PatientDetail>(`/api/patients/${props.detail.patient.patient_id}`, { etag: props.detail.patient.etag, ...changes });
    },
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ["patients"] });
      void queryClient.invalidateQueries({ queryKey: ["consultations"] });
      if (props.mode === "create") {
        const created = data as { patient_id: string; mrn: string };
        props.onSaved({ patient_id: created.patient_id, mrn: created.mrn, name: `${form.first_name} ${form.last_name}`.trim(), dob: form.dob, sex: form.sex });
      } else {
        queryClient.setQueryData(["patient", props.detail.patient.patient_id], data);
        props.onSaved(data as PatientDetail);
      }
    },
  });

  const error = save.error instanceof ApiError ? save.error : null;
  const duplicate = error?.code === "possible_duplicate";
  const set = (key: keyof PatientFields) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [key]: e.target.value });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };
  const unchanged = props.mode === "edit" && Object.entries(form).every(([k, v]) => v === initial[k as keyof PatientFields]);
  const incomplete = !form.first_name.trim() || !form.last_name.trim() || !form.dob;

  return (
    <Modal title={props.mode === "create" ? "Register a new EHR patient" : `Edit ${props.detail.patient.name}`} onClose={props.onClose}>
      {save.isError && (
        <Banner tone={duplicate ? "warn" : "error"} title={duplicate ? "Possible duplicate patient" : errorMessage(save.error)}>
          {duplicate ? errorMessage(save.error) : error?.isConflict ? "Close this window and reopen the patient to see the latest details." : undefined}
        </Banner>
      )}
      {masked.size > 0 && <Banner tone="info" title="Some details are masked for your role">Masked fields are read-only.</Banner>}
      <form onSubmit={submit}>
        <div className="grid grid-2">
          <div className="field"><label htmlFor="pf-first">First name</label><input id="pf-first" className="input" value={form.first_name} onChange={set("first_name")} disabled={locked("first_name")} autoFocus /></div>
          <div className="field"><label htmlFor="pf-last">Last name</label><input id="pf-last" className="input" value={form.last_name} onChange={set("last_name")} disabled={locked("last_name")} /></div>
          <div className="field"><label htmlFor="pf-dob">Date of birth</label><input id="pf-dob" className="input" type="date" max={new Date().toISOString().slice(0, 10)} value={form.dob} onChange={set("dob")} disabled={locked("dob")} /></div>
          <div className="field"><label htmlFor="pf-sex">Sex at birth</label>
            <select id="pf-sex" className="input" value={form.sex} onChange={set("sex")} disabled={locked("sex")}><option>Male</option><option>Female</option><option>Other</option></select>
          </div>
          <div className="field"><label htmlFor="pf-mobile">Mobile</label><input id="pf-mobile" className="input" value={form.mobile} onChange={set("mobile")} disabled={locked("mobile")} /></div>
          <div className="field"><label htmlFor="pf-email">Email</label><input id="pf-email" className="input" type="email" value={form.email} onChange={set("email")} disabled={locked("email")} /></div>
          <div className="field"><label htmlFor="pf-street">Street address</label><input id="pf-street" className="input" value={form.street_address} onChange={set("street_address")} disabled={locked("street_address")} /></div>
          <div className="field"><label htmlFor="pf-city">City</label><input id="pf-city" className="input" value={form.city} onChange={set("city")} disabled={locked("city")} /></div>
          <div className="field"><label htmlFor="pf-state">State</label><input id="pf-state" className="input" value={form.state} onChange={set("state")} disabled={locked("state")} /></div>
          <div className="field"><label htmlFor="pf-zip">ZIP</label><input id="pf-zip" className="input" value={form.zip} onChange={set("zip")} disabled={locked("zip")} /></div>
          <div className="field"><label htmlFor="pf-lang">Preferred language</label><input id="pf-lang" className="input" value={form.preferred_language} onChange={set("preferred_language")} disabled={locked("preferred_language")} /></div>
        </div>
        {props.mode === "create" && duplicate && (
          <label className="row small" style={{ marginBottom: 10 }}>
            <input type="checkbox" checked={allowDuplicate} onChange={(e) => setAllowDuplicate(e.target.checked)} />
            This is a different person: register anyway
          </label>
        )}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn" onClick={props.onClose}>Cancel</button>
          <button className="btn primary" disabled={save.isPending || incomplete || unchanged || (duplicate && !allowDuplicate)}>
            {save.isPending ? "Saving…" : props.mode === "create" ? "Register patient" : "Save changes"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function pick(patient: PatientFields): PatientFields {
  return Object.fromEntries(Object.keys(EMPTY).map((key) => [key, patient[key as keyof PatientFields] ?? ""])) as unknown as PatientFields;
}
