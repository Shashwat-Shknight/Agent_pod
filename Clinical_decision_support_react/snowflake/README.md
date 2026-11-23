# NuuCare on Snowflake

All application data — consultations, AI outputs, approved SOAP notes, coding decisions, the EHR
chart, bills (as PDF), claims, users, audit trail and value-based-care state — is stored in
Snowflake. Nothing is written to local JSON files when `STORAGE_BACKEND=snowflake`.

## Architecture

```
Streamlit UI ──┐                         ┌── CLINICAL  ENCOUNTERS (+ HISTORY, TRANSCRIPTS, AI_OUTPUTS, CODES), SOAP_NOTES
               ├─ app/db repositories ──►│── EHR       SHEETS, STORE_METADATA      (+ V_PATIENTS, V_LABS, ... views)
FastAPI API  ──┘  (pooled, key-pair auth)│── BILLING   GENERATED_BILLS, CLAIMS, BILL_DOCUMENTS (PDF bytes)
                                         │── SECURITY  APP_USERS, ROLE_PERMISSIONS, AUDIT_LOG
                                         │── VBC       STATE_DOCUMENTS
                                         └── META      SCHEMA_MIGRATIONS
```

| Concern | Choice |
|---|---|
| Auth | `TYPE=SERVICE` users with RSA key pairs — no passwords, no MFA prompts. Private keys in `%USERPROFILE%\.snowflake\keys`, outside the repo. |
| Least privilege | `NUUCARE_OWNER` (DDL, migrations only) → `NUUCARE_APP` (DML + procedures, runtime) → `NUUCARE_READONLY` (reporting). Future grants make new objects automatically available. |
| Cost | X-Small warehouse, 60 s auto-suspend, 300 s statement timeout, `NUUCARE_MONITOR` resource monitor (25 credits/month, suspends at 90 %). |
| Schema changes | Versioned, checksummed migrations in `snowflake/migrations` (`V001__...sql`). Applied files cannot be edited — add a new version. |
| Encounter writes | `CLINICAL.UPSERT_ENCOUNTER` stored procedure: one round trip, one transaction, skips unchanged records (SHA-256), appends a version to `ENCOUNTER_HISTORY`, refreshes transcript/AI/code child tables. |
| Lossless storage | Each table keeps the full application object in a `VARIANT` column; hot fields are promoted to typed columns for querying. |
| PHI masking | Dynamic Data Masking + tags (`GOVERNANCE` schema, V004/V005). `NUUCARE_APP`/`NUUCARE_OWNER` see clear data; every other role (incl. `NUUCARE_READONLY`, ACCOUNTADMIN in Snowsight) sees pseudonyms and redactions. Requires Enterprise edition. |
| Audit | `SECURITY.AUDIT_LOG` keeps every event (the in-app view shows the latest 2,000). Encounter history is append-only. SOAP approvals are versioned. |
| Failure mode | If Snowflake is unreachable the UI shows an error and the API returns HTTP 503. Data is never silently written to local disk instead. |
| Documents | Bill and claim PDFs are rendered in memory (ReportLab, reproducible bytes) and stored in `BILLING.BILL_DOCUMENTS`; identical PDFs are stored once. |

## Setup on a new machine / account

See [../OFFICE_SETUP.md](../OFFICE_SETUP.md). In short:

1. `python -m app.db.setup init --account <ORGNAME-ACCOUNTNAME>` - creates this machine's key pairs
   (outside the repo), `.env`, and `snowflake/local/00_bootstrap_run_once.sql` (git-ignored) from
   `snowflake/00_bootstrap_template.sql`, and copies it to the clipboard.
2. Run that SQL once in Snowsight as ACCOUNTADMIN (warehouse, resource monitor, roles, database, service users).
3. `python -m app.db.setup finish` - migrations, seed data, official code sets, knowledge-base index.
4. `python -m app.db.setup check` - status; `GET http://127.0.0.1:8000/health/db`.

## Running the app

```powershell
.venv\Scripts\python -m uvicorn app.main:app --reload        # API
.venv\Scripts\python -m streamlit run frontend/streamlit_app.py
```

## Useful queries

```sql
USE DATABASE NUUCARE_DB;
SELECT * FROM CLINICAL.V_PATIENT_HISTORY ORDER BY ENCOUNTER_AT DESC;         -- every visit, after the patient leaves
SELECT * FROM CLINICAL.ENCOUNTER_HISTORY WHERE ENCOUNTER_ID = 'PT-123456';   -- full change history of one visit
SELECT * FROM CLINICAL.V_CODING_SUMMARY;
SELECT * FROM BILLING.V_BILL_DOCUMENTS ORDER BY CREATED_AT DESC;              -- archived bill / claim PDFs
SELECT * FROM EHR.V_LABS WHERE PATIENT_ID = 'PT-0001';
SELECT * FROM SECURITY.V_AUDIT_RECENT ORDER BY INSERTED_AT DESC;
```

## Tests

```powershell
.venv\Scripts\python -m pytest app/tests                       # unit tests (local JSON backend, no network)
$env:SNOWFLAKE_INTEGRATION=1; .venv\Scripts\python -m pytest app/tests/test_snowflake_integration.py
```

## Going further in production

* Store private keys in a secrets manager (Azure Key Vault / AWS Secrets Manager) and set
  `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` for encrypted keys; rotate with `RSA_PUBLIC_KEY_2`.
* Add a network policy restricting the service users to the app's egress IPs.
* Real PHI requires a Business Critical edition account with a signed BAA — the trial account
  is for synthetic demo data only.
* Increase `DATA_RETENTION_TIME_IN_DAYS` (Time Travel) on Enterprise edition.

## Official code sets (ICD-10-CM, HCPCS) — live, versioned

The coding engine no longer depends on the bundled `ICD10CM_2022_Codes.json`.

| Code set | Source | Refresh |
|---|---|---|
| ICD-10-CM | CDC/NCHS official release files (`ftp.cdc.gov/.../ICD10CM/<year>/`), the files CMS uses for HIPAA claims | Checked daily; new fiscal-year (Oct 1) and April releases applied automatically |
| HCPCS Level II | CMS HCPCS file via the NLM Clinical Tables API | Snapshot refreshed weekly |
| CPT | AMA-licensed — **not loaded** (no licence). CPT codes are reported as *unverified*. | Add a licensed source in `app/reference/sources.py` when available |

* **Schema** `REFERENCE`: `CODES` (one row per code version with `VALID_FROM`/`VALID_TO`), `CODE_SET_RELEASES`,
  `SYNC_RUNS`, views `V_CURRENT_CODES`, `V_CODE_SET_STATUS`, `V_CODE_CHANGES`.
* **Date-of-service aware:** a claim is validated against the release in force on its date of service.
* **Coding engine:** matches against billable codes in force today; a documented category ("Headache",
  "Asthma") resolves to its default billable code (R51.9, J45.909) instead of a non-billable header.
* **Checks:** Coding page → *Official code set* (blocks billing on invalid/non-billable codes, suggests
  replacements); Claim validation → same check plus a live confirmation against NLM.
* **Sync:** automatic in the API process (`REFERENCE_AUTO_SYNC=0` disables, `REFERENCE_SYNC_INTERVAL_HOURS`),
  or manually: `.venv\Scripts\python -m app.reference.sync` · status: `--status`.
* **API:** `GET /reference/status`, `GET /reference/validate?codes=R51,I10&kind=ICD&live=true`,
  `GET /reference/search?q=headache`, `GET /reference/changes?release=FY2027`, `POST /reference/sync`.

```sql
SELECT * FROM NUUCARE_DB.REFERENCE.V_CODE_SET_STATUS;
SELECT * FROM NUUCARE_DB.REFERENCE.V_CODE_CHANGES WHERE RELEASE_ID = 'FY2027';
```
