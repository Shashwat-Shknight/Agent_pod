# NuuCare Agent Pod: React version

Ambient scribe → grounded clinical decision support → coding & HCC → billing & RCM, with a React UI.
This is a copy of `../Clinical_decision_support` (the Streamlit version, unchanged). Both use the same
Snowflake database and record format, so either UI can open work created in the other.

Setup: see [OFFICE_SETUP.md](OFFICE_SETUP.md). Edge cases found and fixed: [EDGE_CASES.md](EDGE_CASES.md).

## How the three parts fit together

```
Browser (React, web/dist)  ──fetch /api/* (cookie + X-NuuCare-Client header)──▶  FastAPI (app/main.py)
                                                                                 ├─ app/web/        API for the UI: sessions, permissions, versions
                                                                                 ├─ app/domain/     business rules (coding, billing, CDS text, access control)
                                                                                 ├─ app/agent/      LLM agents: ambient scribe, CDS (Harrison's RAG)
                                                                                 ├─ code_matcher.py ICD-10-CM / CPT matching (deterministic, no LLM)
                                                                                 └─ app/db/         Snowflake (or local JSON for offline dev)
```

- **One process, one port.** `uvicorn app.main:app --port 8000` serves the API and the built React app.
  No CORS and no second server to start. The office laptop needs no Node.js.
- **The server is the authority.** Every `/api` route checks the signed-in user's role. The React app hides
  what a role cannot do, but hiding is a convenience, never the control. Clinicians are blocked from Codes
  in the permission layer itself (segregation of duties).
- **The original endpoints** (`/encounters/*`, `/codes`, `/ehr/*`, `/patients/*`, `/reference/*`, `/vbc/*`)
  keep their paths but now require a signed-in user. Only `/health` is open.

## The handshake contract (React ↔ API)

| Concern | Rule |
|---|---|
| Errors | Every failure is JSON `{"error": {"code", "message", "fields?"}, "detail"}`: validation 422, permission 403, missing 404, conflict 409, AI or upstream failure 502/503, database down 503 |
| Sign-in | HttpOnly, SameSite=Strict session cookie; a 401 anywhere sends the UI back to sign-in with the reason |
| CSRF | State-changing `/api` calls must send `X-NuuCare-Client: web` (a cross-site form cannot) |
| Concurrency | Records carry a `version`. Writes send the version they saw; a mismatch is 409 `stale_record` → "Reload latest" |
| Idempotency | Creating a consultation consumes a server-held scribe draft id (a double click returns the same consultation); sending a bill twice is 409 `already_sent` |
| Timeouts | 30 s by default; 300 s for scribe/CDS/uploads (the LLM gateway retries internally) |
| Retries | The UI retries reads only on network/5xx errors, never writes |

## Main API routes (`/api`)

| Area | Routes | Permission |
|---|---|---|
| Auth | `GET auth/session`, `POST auth/login`, `auth/logout`, `auth/bootstrap`, `auth/password` | public / signed in |
| Patients | `GET patients?q=&offset=&with_counts=`, `POST patients`, `GET patients/{id}`, `PUT patients/{id}` (etag), `DELETE patients/{id}?etag=` | Clinical Review (View/Edit); deleting a patient with chart history needs EHR Demo Data · Edit |
| Clinical | `POST scribe/text`, `POST scribe/audio`, `POST consultations`, `GET consultations[/{id}]`, `POST consultations/{id}/cds/regenerate`, `.../recommendations/{i}/decision`, `.../plan`, `PUT .../note`, `POST .../insights`, `POST .../sign`, `DELETE consultations/{id}?version=` (drafts only) | Clinical Review (View/Edit/Approve) |
| Coding | `GET coding/queue`, `GET coding/{id}`, `POST coding/{id}/decisions`, `POST coding/{id}/recompute` | Codes |
| Billing | `GET billing/queue`, `GET billing/{id}`, `POST billing/{id}/overrides`, `POST billing/{id}/send`, `GET billing/{id}/pdf` | RCM Workspace |
| Admin | `GET dashboard`, `GET oversight`, `POST consultations/{id}/assign`, `GET/POST/PATCH/DELETE admin/users…`, `GET/PUT admin/roles…`, `GET admin/audit` | Clinical Overview / Admin Dashboard / Super Admin |
| Knowledge | `GET kb/search?q=` | Clinical Review |

Interactive documentation: http://127.0.0.1:8000/docs

## Screens in this version

Sign-in (first-admin setup, forced password change) · Clinical Overview · Consultations (delete drafts) ·
Patients (search, register with duplicate check, edit, delete) · New consultation
(EHR patient search or registration, in-browser WAV recorder, file upload or paste, SOAP draft) ·
Clinical Review (conversation, CDS with accept/modify/override and push-to-plan, Harrison's evidence,
medication safety, care gaps, SOAP edit and sign) · Coding & HCC (queue, code table, evidence and MEAT,
physician query, pre/post validation, audit log) · Billing & RCM (guardrail, coder overrides, draft bill PDF,
attestation, send to RCM with exceptions) · Oversight · User Access (users, role matrix, audit trail) · Knowledge Base.

Not ported yet (still available in the Streamlit version): the value-based-care workspace screens
(the `/vbc` API is still here), IPD billing, claim generation, prior authorization, medication & RTBC,
and OCR superbill import.

## CRUD at a glance

| Data | Create | Read | Update | Delete |
|---|---|---|---|---|
| Patients (EHR) | Register (duplicate name + DOB warning) | Search, paging, detail with consultations | Demographics, with an etag so concurrent edits are a clear 409; consultation headers follow a rename | Only when no consultation uses the patient; chart history needs EHR Demo Data · Edit |
| Consultations | Scribe draft → consultation (idempotent) | List, review | Note, plan, CDS decisions, insights, sign (versioned) | Drafts only, by their author or an admin; the EHR encounter is kept and marked *Cancelled*. Signed notes are never deleted |
| Codes / bills | Matched on first open | Coding and billing screens | Coder decisions, overrides (versioned) | Not deletable: locked once sent to RCM |
| Users | Add (temporary password) | List | Name, email, role, active, password reset, unlock | Delete (not yourself, not the last Super Admin); open work returns to the unassigned queue |

## PHI data masking (two layers, one classification)

| Layer | Who it protects against | How |
|---|---|---|
| **App** (`app/domain/masking.py`) | Signed-in users, per role (HIPAA *minimum necessary*) | Every JSON answer (and bill PDF) is masked for the viewer's role before it leaves the server. Partial values so people can still work: `W*** Y***`, `1962-**-**`, `*****0031`, `(***) ***-1002`, `981**` |
| **Snowflake** (`snowflake/migrations/V004__phi_masking.sql`) | Anyone querying the database directly (reporting role, Snowsight, BI tools) | Dynamic Data Masking policies + `GOVERNANCE.PHI_CLASS` tags on every PHI column. Names become salted pseudonyms (`PSN-1A2B…`), DOB year only (90+ aggregated), IDs last 4, contact and narrative redacted, PDFs and password hashes hidden. Only `NUUCARE_APP` / `NUUCARE_OWNER` see clear data |

Three PHI classes, each unmasked by a normal permission (User Access → Role permissions, action *View*):

| Class | Fields | Unmasked by default for |
|---|---|---|
| PHI: Identity | name, date of birth, MRN, member/group IDs | clinicians, coders, RCM, operations, care managers, pharmacists, abstractors, HEDIS |
| PHI: Contact details | phone, email, street, city, ZIP | clinicians, RCM, care managers |
| PHI: Clinical narrative | conversation, SOAP text, summaries, code evidence | clinicians, coders, care managers, pharmacists, abstractors |

Executives, auditors and Stars/contract users see everything masked; Super Admin sees everything. Rules that come
with it: nobody can save over a value they only see masked (403 `phi_masked`); a role with identity masked can look
patients up by patient ID only (no name search); signing or editing a note needs the narrative class. Stored data
is never changed. The sidebar shows *Masked view* and what is hidden.

Check it in Snowflake: `USE ROLE NUUCARE_READONLY; SELECT PATIENT_NAME, PATIENT_DOB, MRN FROM CLINICAL.ENCOUNTERS;`

## Synthetic demo data

```powershell
.venv\Scripts\python -m app.scripts.seed_synthetic            # 30 patients + 24 consultations at every stage
.venv\Scripts\python -m app.scripts.seed_synthetic --reset    # rebuild
.venv\Scripts\python -m app.scripts.seed_synthetic --remove   # take it all out again
```

It creates synthetic patients **P200001…** with full charts (problems, medications, allergies, vitals, labs,
insurance, care team, appointments), users **syn.doctor, syn.doctor2, syn.coder, syn.rcm, syn.ops**, and runs
consultations from 12 primary-care scenarios through the real API: scribe → CDS → push to plan → sign → code → send
to RCM. Visits are spread over the last three weeks. No LLM or network is needed (the AI steps are scripted), and it
writes to whatever storage `.env` selects. Add `--password <yours>` to choose the syn.* password; otherwise a random
one is printed. On an empty system it also creates `syn.admin` (Super Admin).

## Tests

```powershell
.venv\Scripts\python -m pytest -q app/tests      # 611 backend tests, incl. test_web_api.py, test_crud_api.py, test_seed_synthetic.py, test_masking.py
cd web; npm test                                 # React: every screen rendered with live-captured data + API client contract
```
