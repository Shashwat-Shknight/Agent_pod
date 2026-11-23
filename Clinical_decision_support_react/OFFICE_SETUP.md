# Run NuuCare (React version) on a new machine, e.g. the office laptop

This folder is the React version of NuuCare. One Python server runs everything: the API **and** the
web screens (the React app is already built, in `web/dist`). So the office laptop needs:

- **Python only.** No Node.js, no Hugging Face, no torch, no model downloads, nothing from a CDN.
- Your `.env`, your Snowflake key and your data are not in git, by design.

## Already ran the Streamlit version on this laptop? (2 minutes)

The React version uses the same Snowflake database and the same `.env`.

```powershell
cd C:\Users\<you>\Agent_pod
git pull
cd Clinical_decision_support_react
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy ..\Clinical_decision_support\.env .env
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open **http://127.0.0.1:8000**. Sign in with the same users as before. Done.

## Fresh machine

### 1. Clone and install (about 3 minutes)

```powershell
git clone <repo-url>
cd Agent_pod\Clinical_decision_support_react
py -3.12 -m venv .venv            # Python 3.10+ works; 3.12 is what was tested
.venv\Scripts\python -m pip install -r requirements.txt
```

Behind a strict corporate proxy, add `--proxy http://user@proxy:port` or use your internal PyPI mirror.
For exactly the tested versions: `pip install -r requirements.txt -c constraints.txt`.

### 2. Connect your Snowflake account

```powershell
.venv\Scripts\python -m app.db.setup init --account <ORGNAME-ACCOUNTNAME>
```

Find the account identifier in Snowsight: your name (bottom left) > Account > *View account details*.
This creates this machine's key pair (in `%USERPROFILE%\.snowflake\keys`), writes `.env`, and copies the
bootstrap SQL to your clipboard. Then in https://app.snowflake.com (as ACCOUNTADMIN):
*Projects > Workspaces > + > SQL file*, paste (`Ctrl+V`), *Run All*. The last row says **Bootstrap complete**.

### 3. Add the LLM key

Open `.env` (`notepad .env`) and fill **one** option:

```ini
# Coforge (office)
COFORGE_API_URL=https://...
COFORGE_API_KEY=...
COFORGE_MODEL=...

# or Grok / Groq
GROK_API_KEY="..."
```

Leave `GROK_API_KEY=""` empty to use Coforge.

### 4. Finish (tables, data, official code sets, knowledge base)

```powershell
.venv\Scripts\python -m app.db.setup finish
```

### 5. Run (one terminal)

```powershell
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open **http://127.0.0.1:8000**. On an empty system the first screen creates the Super Admin.
API documentation: http://127.0.0.1:8000/docs

## Good to know

- **Refreshing the page (F5) keeps you signed in.** Sessions end after 30 idle minutes, 12 hours in total,
  or when the server restarts.
- **The microphone** needs `http://127.0.0.1` or `http://localhost` (browsers block it on other plain-http addresses).
- **New users** get a temporary password and must change it at first sign-in.
- **Two people on one encounter:** if someone else changed it, you see "Reload latest" instead of overwriting their work.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Page says "The React UI is not built" | `web/dist` is missing: run `git pull` again (it is committed) |
| "Cannot reach the NuuCare server" | The uvicorn command is not running, or it runs on another port |
| `JWT token is invalid` | The bootstrap SQL has not run in *this* Snowflake account, or `SNOWFLAKE_ACCOUNT` in `.env` is wrong |
| AI note generation fails | LLM settings in `.env` (step 3). `setup check` shows which provider is active |
| Code sets show "bundled snapshot" | Network blocks ftp.cdc.gov / clinicaltables.nlm.nih.gov; the app still works |
| Need offline dev without Snowflake | Set `STORAGE_BACKEND=json` in `.env` |

## Changing the screens (developers only, needs Node.js 18+)

```powershell
cd web
npm install
npm run dev        # http://localhost:5173, talks to the API on :8000
npm test           # screen + API-client tests
npm run build      # writes web/dist; commit it so other machines need no Node
```
