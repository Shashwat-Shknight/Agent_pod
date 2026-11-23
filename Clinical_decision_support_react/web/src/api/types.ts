// Shapes returned by app/web/*_routes.py. Keep in sync with the server.

export type Action = "View" | "Edit" | "Approve" | "Admin";
export type Permissions = Record<string, Record<Action, boolean>>;

export interface Profile {
  username: string;
  name: string;
  email: string;
  role: string;
  must_change_password: boolean;
  permissions: Permissions;
  pages: string[];
}

export type SessionState = "bootstrap" | "signed_out" | "password_change" | "ready";

export interface SessionResponse {
  state: SessionState;
  user: Profile | null;
  notice?: string;
}

export interface RecordSummary {
  id: string;
  version: number;
  name: string;
  mrn: string;
  patient_id: string;
  encounter_id: string;
  doctor: string;
  date: string;
  time: string;
  status: "Pending" | "Approved" | string;
  approval_date: string;
  assigned_to: string | null;
  coding_status: string;
  bill_generated_at: string;
  stage: "Ambient Scribe" | "Clinical Review" | "Coding" | "Billed" | string;
  decided?: number;
}

export interface DashboardResponse {
  kpis: { total: number; today: number; pending: number; signed: number; coded: number; sign_off_rate: number };
  activity: { date: string; consults: number; signed: number }[];
  queue: RecordSummary[];
  ehr: { connected: boolean; patients?: number; error?: string };
}

export interface PatientRow {
  patient_id: string;
  name: string;
  mrn: string;
  dob: string;
  sex: string;
  consultations?: number;
  city?: string;
}

export interface PatientList {
  patients: PatientRow[];
  total: number;
  offset: number;
  limit: number;
}

/** Editable demographics (API field names). */
export interface PatientFields {
  first_name: string;
  last_name: string;
  dob: string;
  sex: string;
  mobile: string;
  email: string;
  street_address: string;
  city: string;
  state: string;
  zip: string;
  preferred_language: string;
}

export interface PatientDetail {
  patient: PatientFields & { patient_id: string; mrn: string; name: string; status: string; etag: string };
  consultations: RecordSummary[];
  chart_rows: number;
  permissions: { edit: boolean; delete: boolean; delete_reason: string; masked?: string[] };
}

export interface ChartContext {
  problems: string;
  medications: string;
  allergies: string;
  vitals: string;
  labs: string;
}

export interface PatientContext {
  patient: Record<string, unknown>;
  context: ChartContext;
  coverage: { payer: string; plan: string; status: string };
  allergies: string[];
}

export interface TranscriptEntry {
  Speaker: string;
  Text: string;
}

export interface ScribeResult {
  draft_id: string;
  transcript: string;
  entries: TranscriptEntry[];
  soap_note: string;
  key_highlights: string[];
  sections: Record<string, string>;
  word_count: number;
}

export interface CdsItem {
  title: string;
  body: string;
  priority: "HIGH" | "MED" | "ROUTINE";
}

export interface CdsDecision {
  status: "accepted" | "modified" | "overridden";
  note: string;
  at: string;
  title: string;
  by?: string;
}

export interface Check {
  label: string;
  status: string;
  tone: "green" | "amber" | "red" | "gray" | string;
  detail: string;
}

export interface ReviewView {
  record: {
    id: string;
    name: string;
    mrn?: string;
    dob?: string;
    doctor: string;
    date: string;
    time: string;
    status: string;
    summary: string;
    transcript: string;
    transcript_data?: TranscriptEntry[];
    key_highlights?: string[];
    manual_insights?: string[];
    review_history?: { at: string; action: string }[];
    signed_by?: string;
    approval_date?: string;
    ehr_patient_id?: string;
    ehr_encounter_id?: string;
    [key: string]: unknown;
  };
  summary: RecordSummary;
  cds: {
    items: CdsItem[];
    reasoning: string;
    safety_note: string;
    passages: { source: string; text: string }[];
    decisions: Record<string, CdsDecision>;
  };
  chart: { linked: boolean; context: ChartContext | null; coverage: string };
  medication_safety: Check[];
  care_gaps: Check[];
  insights: { name: string; category: string }[];
  handoff: string;
  permissions: { edit: boolean; sign: boolean };
  master_insights: string[];
  warnings?: string[];
}

export interface ValidationCheck {
  name: string;
  status: "pass" | "warn" | "fail";
  detail: string;
}

export interface CheckSummary {
  checks: ValidationCheck[];
  status: "pass" | "warn" | "fail";
  label: string;
  score: number;
  total: number;
  fails: number;
  warnings: number;
}

export interface CodeRow {
  key: string;
  type: string;
  code: string;
  term: string;
  description: string;
  hcc: string;
  match: number | null;
  status: string;
  excerpt: string;
  procedure_status?: string;
  meat?: Record<"M" | "E" | "A" | "T", boolean>;
  cdi?: boolean;
  mdm?: string;
}

export interface CodingView {
  summary: RecordSummary;
  note: { signed: boolean; text: string; signed_by: string; approval_date: string };
  rows: CodeRow[];
  pre_checks: CheckSummary;
  post_checks: CheckSummary;
  totals: {
    icd: number;
    cpt: number;
    accepted: number;
    rejected: number;
    queries: number;
    hccs: string[];
    em_code: string | null;
    mdm: string;
  };
  audit: { at: string; code: string; action: string; note: string; by: string }[];
  can_edit: boolean;
  matcher_version: string;
}

export interface GuardrailRow {
  code: string;
  service: string;
  order: string;
  evidence: string;
  documentation: string;
  guardrail: "BILLABLE" | "BLOCKED" | "UNVERIFIED" | "EXCLUDED";
  tone: string;
  reason: string;
  overridden: boolean;
  overridable: boolean;
}

export interface BillRow {
  "CPT/HCPCS Code": string;
  "Service Description": string;
  Department: string;
  Quantity: number;
  "Unit Charge": string;
  "Gross Charge": string;
  "Pricing Status": "Priced" | "Unpriced";
}

export interface BillingView {
  summary: RecordSummary;
  encounter_type: string;
  guardrail: GuardrailRow[];
  bill_rows: BillRow[];
  total: number;
  held_value: number;
  unpriced: string[];
  post_checks: CheckSummary | null;
  exceptions: { check: string; detail: string }[];
  notice: string;
  sent: { at: string; by: string; exceptions: { check: string; detail: string }[] } | null;
  blockers: string[];
  can_override: boolean;
  can_send: boolean;
}

export interface OversightRow {
  Patient: string;
  MRN: string;
  "Patient ID": string;
  Encounter: string;
  Workflow: string;
  Stage: string;
  Owner: string;
  Updated: string;
  "Pending item": string;
  Priority: string;
  record_id: string | null;
  version: number;
}

export interface UserRow {
  username: string;
  name: string;
  email: string;
  role: string;
  active: boolean;
  out_of_office: boolean;
  fallback: string | null;
  must_change_password: boolean;
  last_login: string | null;
  created_at: string;
  locked: boolean;
}

export interface RoleMatrix {
  roles: string[];
  modules: string[];
  actions: Action[];
  matrix: Record<string, Record<string, Partial<Record<Action, boolean>>>>;
  locked: Record<string, string>;
}
