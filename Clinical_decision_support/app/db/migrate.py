"""Versioned schema migrations for Snowflake.

Usage (from the Clinical_decision_support folder):
    .venv\\Scripts\\python -m app.db.migrate            apply pending migrations
    .venv\\Scripts\\python -m app.db.migrate --status   show applied / pending

Migrations are ``snowflake/migrations/V<NNN>__<description>.sql``. Each runs once, as the deploy
service user (NUUCARE_OWNER). Applied files are checksummed; editing an applied migration is
refused - add a new V<NNN+1> file instead.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from io import StringIO
from pathlib import Path

from app.db import client
from app.db.settings import PROJECT_ROOT

MIGRATIONS_DIR = PROJECT_ROOT / "snowflake" / "migrations"
_NAME = re.compile(r"^V(\d{3,})__([A-Za-z0-9_]+)\.sql$")

_LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS META COMMENT = 'Deployment metadata';
CREATE TABLE IF NOT EXISTS META.SCHEMA_MIGRATIONS (
    VERSION      NUMBER(6)     NOT NULL,
    DESCRIPTION  STRING        NOT NULL,
    SCRIPT       STRING        NOT NULL,
    CHECKSUM     STRING        NOT NULL,
    APPLIED_BY   STRING        NOT NULL DEFAULT CURRENT_USER(),
    APPLIED_AT   TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    DURATION_MS  NUMBER(12),
    CONSTRAINT PK_SCHEMA_MIGRATIONS PRIMARY KEY (VERSION)
);
"""


def discover() -> list[dict]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("V*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise SystemExit(f"Bad migration filename: {path.name} (expected V001__description.sql)")
        text = path.read_text(encoding="utf-8")
        migrations.append({
            "version": int(match.group(1)),
            "description": match.group(2).replace("_", " "),
            "script": path.name,
            "sql": text,
            "checksum": hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest(),
        })
    versions = [item["version"] for item in migrations]
    if len(versions) != len(set(versions)):
        raise SystemExit("Duplicate migration version numbers")
    return migrations


def _execute_script(connection, sql: str) -> None:
    for cursor in connection.execute_stream(StringIO(sql), remove_comments=True):
        cursor.close()


def applied(connection) -> dict[int, dict]:
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT VERSION, SCRIPT, CHECKSUM, APPLIED_AT FROM META.SCHEMA_MIGRATIONS ORDER BY VERSION")
        return {int(row[0]): {"script": row[1], "checksum": row[2], "applied_at": row[3]} for row in cursor.fetchall()}
    finally:
        cursor.close()


def run(status_only: bool = False) -> int:
    import time

    migrations = discover()
    with client.pool("deploy").connection() as connection:
        _execute_script(connection, _LEDGER_DDL)
        done = applied(connection)
        for item in migrations:
            previous = done.get(item["version"])
            if previous and previous["checksum"] != item["checksum"]:
                raise SystemExit(
                    f"{item['script']} was modified after it was applied. "
                    "Create a new migration instead of editing an applied one."
                )
        pending = [item for item in migrations if item["version"] not in done]
        for item in migrations:
            state = f"applied {done[item['version']]['applied_at']}" if item["version"] in done else "PENDING"
            print(f"  V{item['version']:03d}  {item['description']:<45} {state}")
        if status_only or not pending:
            print("Schema is up to date." if not pending else f"{len(pending)} migration(s) pending.")
            return 0
        for item in pending:
            print(f"Applying {item['script']} ...", flush=True)
            started = time.monotonic()
            _execute_script(connection, item["sql"])
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "INSERT INTO META.SCHEMA_MIGRATIONS (VERSION, DESCRIPTION, SCRIPT, CHECKSUM, DURATION_MS) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (item["version"], item["description"], item["script"], item["checksum"],
                     int((time.monotonic() - started) * 1000)),
                )
            finally:
                cursor.close()
            print(f"  done in {time.monotonic() - started:.1f}s")
    print("All migrations applied.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="show migration status without applying")
    args = parser.parse_args(argv)
    try:
        return run(status_only=args.status)
    except client.StorageError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
