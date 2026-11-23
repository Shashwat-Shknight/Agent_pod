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
| Clinical | `GET patients`, `POST patients`, `POST scribe/text`, `POST scribe/audio`, `POST consultations`, `GET consultations[/{id}]`, `POST consultations/{id}/cds/regenerate`, `.../recommendations/{i}/decision`, `.../plan`, `PUT .../note`, `POST .../insights`, `POST .../sign` | Clinical Review (View/Edit/Approve) |
| Coding | `GET coding/queue`, `GET coding/{id}`, `POST coding/{id}/decisions`, `POST coding/{id}/recompute` | Codes |
| Billing | `GET billing/queue`, `GET billing/{id}`, `POST billing/{id}/overrides`, `POST billing/{id}/send`, `GET billing/{id}/pdf` | RCM Workspace |
| Admin | `GET dashboard`, `GET oversight`, `POST consultations/{id}/assign`, `GET/POST/PATCH admin/users…`, `GET/PUT admin/roles…`, `GET admin/audit` | Clinical Overview / Admin Dashboard / Super Admin |
| Knowledge | `GET kb/search?q=` | Clinical Review |

Interactive documentation: http://127.0.0.1:8000/docs

## Screens in this version

Sign-in (first-admin setup, forced password change) · Clinical Overview · Consultations · New consultation
(EHR patient search or registration, in-browser WAV recorder, file upload or paste, SOAP draft) ·
Clinical Review (conversation, CDS with accept/modify/override and push-to-plan, Harrison's evidence,
medication safety, care gaps, SOAP edit and sign) · Coding & HCC (queue, code table, evidence and MEAT,
physician query, pre/post validation, audit log) · Billing & RCM (guardrail, coder overrides, draft bill PDF,
attestation, send to RCM with exceptions) · Oversight · User Access (users, role matrix, audit trail) · Knowledge Base.

Not ported yet (still available in the Streamlit version): the value-based-care workspace screens
(the `/vbc` API is still here), IPD billing, claim generation, prior authorization, medication & RTBC,
and OCR superbill import.

## Tests

```powershell
.venv\Scripts\python -m pytest -q app/tests      # 586 backend tests, incl. test_web_api.py (auth, roles, conflicts, hand-off)
cd web; npm test                                 # React: every screen rendered with live-captured data + API client contract
```
