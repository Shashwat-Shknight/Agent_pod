"""Database health report.

    .venv\\Scripts\\python -m app.db.status
"""

from __future__ import annotations

import sys

from app.db import client
from app.db.settings import snowflake_settings, storage_backend

TABLES = [
    "CLINICAL.ENCOUNTERS", "CLINICAL.ENCOUNTER_HISTORY", "CLINICAL.ENCOUNTER_TRANSCRIPTS",
    "CLINICAL.ENCOUNTER_AI_OUTPUTS", "CLINICAL.ENCOUNTER_CODES", "CLINICAL.SOAP_NOTES",
    "EHR.SHEETS", "BILLING.GENERATED_BILLS", "BILLING.BILL_DOCUMENTS", "BILLING.CLAIMS",
    "SECURITY.APP_USERS", "SECURITY.ROLE_PERMISSIONS", "SECURITY.AUDIT_LOG", "VBC.STATE_DOCUMENTS",
]


def main() -> int:
    print(f"STORAGE_BACKEND = {storage_backend()}")
    settings = snowflake_settings()
    print(f"Account {settings.account}, user {settings.user}, role {settings.role}, "
          f"warehouse {settings.warehouse}, database {settings.database}")
    try:
        info = client.ping()
        print(f"Connected: Snowflake {info.get('VERSION')} as {info.get('USER_NAME')} ({info.get('ROLE_NAME')})")
        union = " UNION ALL ".join(f"SELECT '{name}' AS T, COUNT(*) AS N FROM {name}" for name in TABLES)
        for row in client.query(union):
            print(f"  {row['T']:<34} {row['N']:>8}")
        migrations = client.query("SELECT COUNT(*) AS N, MAX(VERSION) AS V FROM META.SCHEMA_MIGRATIONS")
        print(f"Migrations applied: {migrations[0]['N']} (latest V{int(migrations[0]['V'] or 0):03d})")
    except client.StorageError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
