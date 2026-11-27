# Run NuuCare on a new machine (e.g. office laptop)

Everything needed is in the repository: backend (FastAPI), frontend (Streamlit), Snowflake schema
and migrations, the official ICD-10-CM / HCPCS code-set sync (with an offline snapshot), and the
knowledge base. **No Hugging Face, torch or model downloads are needed.**

Not in the repository (by design): your `.env`, your Snowflake keys, and your data.

## 1. Clone and install (about 3 minutes)

```powershell
git clone <repo-url>
cd Agent_pod\Clinical_decision_support
py -3.12 -m venv .venv            # Python 3.10+ works; 3.12 is what was tested
.venv\Scripts\python -m pip install -r requirements.txt
```

Behind a strict corporate proxy, add `--proxy http://user@proxy:port` or use your internal PyPI mirror.
For exactly the tested versions: `pip install -r requirements.txt -c constraints.txt`.

## 2. Connect your Snowflake account

```powershell
.venv\Scripts\python -m app.db.setup init --account <ORGNAME-ACCOUNTNAME>
```

Find the account identifier in Snowsight: your name (bottom left) > Account > *View account details*.

This creates this machine's key pair (in `%USERPROFILE%\.snowflake\keys`, outside the project),
writes `.env`, and copies the bootstrap SQL to your clipboard. Then:

1. Open https://app.snowflake.com as a user with **ACCOUNTADMIN**
2. *Projects > Workspaces > + > SQL file*, paste (`Ctrl+V`), *Run All* (`Ctrl+Shift+Enter`)
3. The last result row says **Bootstrap complete**

## 3. Add the LLM key

Open `.env` and fill **one** option:

```ini
# Coforge (office)
COFORGE_API_URL=https://...
COFORGE_API_KEY=...
COFORGE_MODEL=...

# or Grok / Groq
GROK_API_KEY="..."
```

Leave `GROK_API_KEY=""` empty to use Coforge.

## 4. Finish (tables, data, official code sets, knowledge base)

```powershell
.venv\Scripts\python -m app.db.setup finish
```

This applies the database migrations, loads the demo EHR chart, syncs ICD-10-CM (CDC) and HCPCS
(CMS via NLM), and builds the offline knowledge-base index. If the office network blocks CDC/NLM,
the bundled snapshot in `data/reference/` is loaded instead and the live sync takes over once the
sites are reachable.

Check everything anytime: `.venv\Scripts\python -m app.db.setup check`

## 5. Run

Two terminals in `Clinical_decision_support`:

```powershell
.venv\Scripts\python -m uvicorn app.main:app --port 8000
.venv\Scripts\python -m streamlit run frontend/streamlit_app.py
```

Open http://localhost:8501. The first sign-in creates the Super Admin. API docs: http://127.0.0.1:8000/docs

## Updating an existing install to the diagnostics / RCM branch

On a machine that already runs NuuCare (Snowflake connected, `.env` in place):

```powershell
cd Agent_pod
git fetch https://github.com/Shashwat-Shknight/Agent_pod.git feature/diagnostics-rcm-prior-auth
git checkout -b feature/diagnostics-rcm-prior-auth FETCH_HEAD
cd Clinical_decision_support
.venv\Scripts\python -m app.db.migrate      # optional: adds the V006 reporting views (views only)
```

Then restart both terminals. No new packages and no data load are needed: the new sheets (orders,
prior authorizations, benefits, patient accounts) are created in Snowflake the first time they are
used. Existing patients keep their MRNs; new visits get `ENC#########` encounter numbers. See
[RCM_DIAGNOSTICS.md](RCM_DIAGNOSTICS.md) for the walkthrough.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `JWT token is invalid` | The bootstrap SQL has not run in *this* Snowflake account, or `SNOWFLAKE_ACCOUNT` in `.env` is wrong |
| `Invalid account ... subdomain` | Use only `ORGNAME-ACCOUNTNAME`, no `https://` or `.snowflakecomputing.com` |
| AI note generation fails | LLM settings in `.env` (step 3). `setup check` shows which provider is active |
| Code sets show "bundled snapshot" | Network blocks ftp.cdc.gov / clinicaltables.nlm.nih.gov; the app still works |
| Need offline dev without Snowflake | Set `STORAGE_BACKEND=json` in `.env` |

Optional dense-vector retrieval (only where Hugging Face is allowed):
`pip install -r requirements-ml.txt`, set `EMBEDDING_BACKEND=sentence_transformer`, run `python -m app.scripts.build_kb`.
