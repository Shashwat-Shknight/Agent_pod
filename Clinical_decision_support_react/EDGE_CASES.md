# Edge cases found and fixed in the React version

Each item names the problem in the Streamlit version, what happens now, and the test that proves it.

## Security and access

| # | Problem before | Now | Test |
|---|---|---|---|
| 1 | Every backend endpoint (`/patients`, `/encounters/*`, `/codes`, `/ehr/*`, `/vbc/*`) was open to anyone who could reach port 8000. Permissions existed only in the Streamlit screens | All routes need a signed-in, active user; each `/api` route checks the role on the server. Only `/health` is open | `test_unauthenticated_and_csrf_requests_are_refused` |
| 2 | Clinician blocked from Codes only in the UI and the role matrix | Blocked in the API; coding fields are stripped from the clinical response; a role-matrix edit cannot grant it | `test_clinicians_never_reach_codes_or_billing` |
| 3 | No CSRF protection | SameSite=Strict cookie and a required client header on every write | same as 1 |
| 4 | A deactivated or re-roled user kept working until their browser tab closed | Their sessions end immediately | `test_deactivated_user_is_signed_out_immediately` |
| 5 | Admin could deactivate themselves or remove their own role | Refused (`self_lockout`) | `test_admin_cannot_lock_themselves_out` |
| 6 | Two first visitors could both create a Super Admin | The bootstrap check runs again under a lock | `test_session_state_drives_bootstrap_then_sign_in` |
| 7 | Password reset left other sessions alive | A reset or password change signs out the other devices | `reset_password`, `change_password` |
| 8 | F5 signed the user out (session lived in the tab) | Server-side session cookie: refresh keeps you in; 30 min idle and 12 h absolute limits | `test_sessions_expire_after_idle_time` |

## Data integrity and concurrency

| # | Problem before | Now | Test |
|---|---|---|---|
| 9 | Two people on one encounter: the last save silently overwrote the other's work | Version check on every write; 409 + "Reload latest" | `test_stale_version_conflict_and_signed_note_lock` |
| 10 | Double-clicking "Proceed to CDS" could create two consultations | The scribe draft is held on the server and consumed once; a repeat returns the same consultation | `test_create_is_idempotent_and_drafts_are_private` |
| 11 | Sending a bill twice created two claims | 409 `already_sent` | `test_full_handoff_doctor_coder_rcm` |
| 12 | Coding could change after the bill was released | Coding is locked once sent to RCM | same |
| 13 | A bill could go to RCM from an unsigned note | 409 `note_unsigned`; the UI lists blockers | same |
| 14 | Signing an incomplete note archived nothing but still signed | Signing requires all SOAP sections | same as 9 |
| 15 | "Push to note" twice duplicated the plan line | Already-present lines are skipped, with a notice | same as 9 |
| 16 | Signed notes could still be edited through some paths | All edits refuse a signed note (409 `note_signed`) | same as 9 |
| 17 | Users and permissions were saved from a stale in-memory copy | Each change re-reads and saves under a lock | `store_transaction` |
| 18 | Override or modify of a CDS item without a reason | A reason or wording is required | `test_recommendation_override_needs_a_reason` |
| 19 | Decisions on codes or recommendations that no longer exist (after re-match or regenerate) | 404/422 with "reload" | `test_full_handoff…`, `test_recommendation_override…` |

## Clinical correctness

| # | Problem before | Now | Test |
|---|---|---|---|
| 20 | "Patient **denies** chest pain" or "**no** fever or cough" still produced those codes | Mentions negated in their own clause are ignored; "history of" (chronic, active) is kept; family history ("mother has asthma") is ignored | `test_negation.py` |
| 21 | The coding screen called the backend over HTTP and kept a second, drifting copy of the matcher as a fallback | One shared matching function, called in-process | `test_cpt_performed_status.py`, `test_billing_eligibility.py` |
| 22 | E11.42 / E11.65 showed no HCC | Mapped to HCC 37 / HCC 38 (CMS-HCC V28 demo map) | `test_full_handoff…` |
| 23 | VBC trend chart reused a cached result after a reset (same revision number) | The cache key includes a unique state id | `test_vbc_poc.py` |
| 24 | VBC evidence decisions were dated by the wall clock but evaluated on the business date, so a verified result could be ignored in the trend | Decisions carry the business date they count from | `test_dashboard_baseline_and_improvement` (was failing before) |

## Front-end ↔ back-end handshake

| # | Problem before | Now | Test |
|---|---|---|---|
| 25 | Errors came back in mixed shapes (FastAPI `detail`, plain text, HTML 500) | One JSON error envelope for every failure; unhandled errors are logged and return a clean 500 | `test_validation_errors_use_the_common_envelope` |
| 26 | An unknown API path could be answered by the web page | API paths 404 as JSON; only screen paths get the React page | `test_unknown_api_path_is_json_404_not_the_react_page` |
| 27 | Browser microphone produced WebM, which the speech engine cannot read | The React recorder encodes 16 kHz WAV in the browser; uploads are checked by content (not extension), size-limited to 25 MB, with clear errors for empty, silent or unsupported audio | `test_scribe_rejects_empty_and_bad_audio`, `test_silent_wav_…`, `wav encoder` (web) |
| 28 | LLM failures surfaced as raw tracebacks | Mapped to clear messages: not configured (503), unreachable (503), unreadable answer (502) | `_llm` in `clinical_routes.py` |
| 29 | A CDS failure while creating a consultation lost the work | The consultation is saved with a warning and "Regenerate CDS" | `test_cds_failure_still_saves_the_consultation` |
| 30 | An EHR sync failure after saving a note failed the user's save | The save stands; the sync problem is shown as a warning | `ehr.sync_clinical_note` |
| 31 | Requests could hang forever | Client timeouts (30 s; 300 s for AI); timeout, network and HTML-instead-of-JSON each give a clear message | `times out instead of hanging forever`, `reports an HTML page…` (web) |
| 32 | A 401 on one screen left other screens showing the previous user's data | Any 401 clears all cached data and returns to sign-in with the server's reason | `notifies the auth layer on 401…` (web) |
| 33 | Server down showed a blank or broken page | A "Cannot reach the NuuCare server" screen with Retry and the start command | `explains a server that is down…` (web) |
| 34 | Closing the tab lost an unsaved SOAP edit or scribe draft | The browser asks before leaving | `Review.tsx`, `NewConsultation.tsx` |
| 35 | Transcripts of any size were sent to the LLM | Minimum 40 and maximum 60,000 characters, with clear messages | `test_scribe_rejects_empty_and_bad_audio` |
| 36 | New patient registration accepted future dates of birth | Validated on the server (past date, after 1900) | `test_validation_errors_use_the_common_envelope` |
