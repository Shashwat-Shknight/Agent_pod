"""One-command setup of NuuCare on a new machine / Snowflake account.

Step 1 (creates keys, .env and your bootstrap SQL):
    .venv\\Scripts\\python -m app.db.setup init --account <ORGNAME-ACCOUNTNAME>

Step 2: paste the bootstrap SQL (already on your clipboard) into a Snowflake worksheet and Run All.

Step 3 (tables, data, official code sets, knowledge base):
    .venv\\Scripts\\python -m app.db.setup finish

Check anytime:
    .venv\\Scripts\\python -m app.db.setup check
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from app.db.settings import ENV_FILE, PROJECT_ROOT, reset_cache

KEY_DIR = Path.home() / ".snowflake" / "keys"
KEYS = {"APP": "nuucare_app_svc", "DEPLOY": "nuucare_deploy_svc"}
TEMPLATE = PROJECT_ROOT / "snowflake" / "00_bootstrap_template.sql"
GENERATED = PROJECT_ROOT / "snowflake" / "local" / "00_bootstrap_run_once.sql"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9]+[-_][A-Za-z0-9_]+$")


def _generate_key(name: str) -> str:
    """Create an RSA-2048 key pair (PKCS#8, unencrypted) if missing; return the public key body."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_path, public_path = KEY_DIR / f"{name}.p8", KEY_DIR / f"{name}.pub"
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    if not private_path.is_file():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        public_path.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        _restrict(private_path)
        print(f"  created key pair {private_path}")
    else:
        print(f"  reusing existing key {private_path}")
        if not public_path.is_file():
            key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
            public_path.write_bytes(key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    lines = public_path.read_text(encoding="ascii").strip().splitlines()
    return "".join(line for line in lines if "-----" not in line)


def _restrict(path: Path) -> None:
    """Private key readable by the current user only."""
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                       capture_output=True, check=False)
    else:
        path.chmod(0o600)


def _copy_to_clipboard(text: str) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                           input=text, text=True, check=True, capture_output=True)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
        else:
            return False
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _write_env(account: str) -> None:
    if ENV_FILE.is_file():
        text = ENV_FILE.read_text(encoding="utf-8")
        if re.search(r"^SNOWFLAKE_ACCOUNT=\S+", text, flags=re.M) and "<ORGNAME-ACCOUNTNAME>" not in text:
            text = re.sub(r"^SNOWFLAKE_ACCOUNT=.*$", f"SNOWFLAKE_ACCOUNT={account}", text, flags=re.M)
            ENV_FILE.write_text(text, encoding="utf-8")
            print(f"  updated SNOWFLAKE_ACCOUNT in existing {ENV_FILE.name}")
            return
    text = ENV_EXAMPLE.read_text(encoding="utf-8").replace("<ORGNAME-ACCOUNTNAME>", account)
    text = text.replace("# Copy to .env (never commit .env).", "# Local configuration - DO NOT COMMIT (git-ignored).")
    ENV_FILE.write_text(text, encoding="utf-8")
    print(f"  created {ENV_FILE.name} (add your LLM key: COFORGE_* or GROK_API_KEY)")


def init(account: str) -> int:
    account = account.strip().upper()
    if not ACCOUNT_PATTERN.match(account) or "." in account or "/" in account:
        print("ERROR: --account must be the account identifier only, e.g. ABCDEFG-XY12345 "
              "(Snowsight > your name > Account > View account details). No https://, no .snowflakecomputing.com.")
        return 2
    print("1/3 Key pairs (stored outside the project, in your user profile)")
    public = {role: _generate_key(name) for role, name in KEYS.items()}
    print("2/3 Local configuration")
    _write_env(account)
    print("3/3 Bootstrap SQL")
    sql = TEMPLATE.read_text(encoding="utf-8")
    sql = sql.replace("{{APP_PUBLIC_KEY}}", public["APP"]).replace("{{DEPLOY_PUBLIC_KEY}}", public["DEPLOY"])
    GENERATED.parent.mkdir(parents=True, exist_ok=True)
    GENERATED.write_text(sql, encoding="utf-8")
    copied = _copy_to_clipboard(sql)
    shown = GENERATED.relative_to(PROJECT_ROOT) if GENERATED.is_relative_to(PROJECT_ROOT) else GENERATED
    print(f"  wrote {shown}" + (" and copied it to the clipboard" if copied else ""))
    print("\nNEXT:\n"
          "  a) Open https://app.snowflake.com (sign in as an ACCOUNTADMIN user)\n"
          "  b) Projects > Workspaces > + > SQL file, paste (Ctrl+V), Run All (Ctrl+Shift+Enter)\n"
          "     - last result row should say 'Bootstrap complete'\n"
          "  c) Then run:  .venv\\Scripts\\python -m app.db.setup finish")
    return 0


def finish() -> int:
    reset_cache()
    from app.db import client, import_local, migrate

    print("== Connectivity")
    try:
        info = client.ping()
    except client.StorageError as error:
        print(f"ERROR: cannot connect as the app service user: {error}\n"
              "Did the bootstrap SQL run successfully in Snowflake, and is SNOWFLAKE_ACCOUNT in .env correct?")
        return 1
    print(f"  connected as {info.get('USER_NAME')} ({info.get('ROLE_NAME')})")
    print("== Schema migrations")
    if migrate.run() != 0:
        return 1
    print("== Seed data (EHR chart, approved notes, local records)")
    import_local.main([])
    print("== Official code sets (ICD-10-CM, HCPCS) - live, or bundled snapshot if the network blocks CDC/NLM")
    from app.reference import sync

    for result in sync.sync_all("setup"):
        print(f"  {result['system']:<8} {result['status']:<11} {result.get('message', '')}")
    print("== Knowledge base index (TF-IDF, offline)")
    from app.core.config import KB_BACKEND_FILE

    if not KB_BACKEND_FILE.exists():
        from app.scripts.build_kb import build_knowledge_base

        build_knowledge_base()
    print("  ready")
    print("\nDone. Start the app:\n"
          "  .venv\\Scripts\\python -m uvicorn app.main:app --port 8000\n"
          "  then open http://127.0.0.1:8000 in the browser (the same server serves the React UI)")
    return 0


def check() -> int:
    from app.db import status

    code = status.main()
    try:
        from app.reference import sync

        for row in sync.status():
            print(f"  code set {row['CODE_SYSTEM']}: {row['ACTIVE_RELEASE']} ({row['BILLABLE_IN_FORCE']} billable)")
    except Exception as error:  # noqa: BLE001
        print(f"  code sets: unavailable ({error})")
    from app.llm_client import _grok_settings
    from app.core import config

    llm = "Grok/Groq" if _grok_settings() else ("Coforge" if config.COFORGE_API_KEY else "NOT CONFIGURED")
    print(f"  LLM provider: {llm}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init", help="create keys, .env and the bootstrap SQL")
    init_parser.add_argument("--account", required=True, help="Snowflake account identifier, e.g. ABCDEFG-XY12345")
    sub.add_parser("finish", help="after the bootstrap SQL ran: migrate, seed, sync code sets")
    sub.add_parser("check", help="show configuration and database status")
    args = parser.parse_args(argv)
    if args.command == "init":
        return init(args.account)
    return finish() if args.command == "finish" else check()


if __name__ == "__main__":
    raise SystemExit(main())
