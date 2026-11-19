"""One-time import of existing local data into Snowflake.

    .venv\\Scripts\\python -m app.db.import_local            import everything not yet in Snowflake
    .venv\\Scripts\\python -m app.db.import_local --dry-run  only report what would be imported

Idempotent: data already present in Snowflake is never overwritten. Local files are left in
place (read-only from now on) so nothing is lost; once verified they can be deleted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.db import access_store, client, ehr_store, encounters, soap_notes, vbc_state
from app.db.settings import PROJECT_ROOT

DATA = PROJECT_ROOT / "data"
SOURCES = {
    "encounters": PROJECT_ROOT / "clinical_records.json",
    "soap_notes": DATA / "approved_soap_notes",
    "access": DATA / "access_control.json",
    "ehr": PROJECT_ROOT / "Epic_Inspired_USA_50_Patient_Demo_Athena_Care_Gap.json",
    "vbc_state": DATA / "vbc_state.json",
    "vbc_poc_state": DATA / "vbc_poc_state.json",
}


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def import_encounters(dry_run: bool) -> str:
    path = SOURCES["encounters"]
    if not path.is_file():
        return "no local clinical_records.json"
    payload = _read_json(path)
    records = payload.get("records", []) if isinstance(payload, dict) else []
    existing = {record.get("id") for record in encounters.list_encounters()}
    new = [record for record in records if isinstance(record, dict) and record.get("id") and record["id"] not in existing]
    if not dry_run:
        # Oldest first so CREATED_AT ordering matches the original newest-first list.
        for record in reversed(new):
            encounters.save_encounter(record, actor="import_local")
    return f"{len(new)} of {len(records)} consultation(s) imported"


def import_soap_notes(dry_run: bool) -> str:
    folder = SOURCES["soap_notes"]
    files = sorted(folder.glob("*_soap.json")) if folder.is_dir() else []
    count = 0
    for path in files:
        payload = _read_json(path)
        if not isinstance(payload, dict) or payload.get("status") != "approved" or not payload.get("encounter_id"):
            continue
        if soap_notes.load_latest(payload["encounter_id"]) is not None:
            continue
        count += 1
        if not dry_run:
            soap_notes.save_approved({
                "encounter_id": str(payload["encounter_id"]),
                "patient_id": str(payload.get("patient_id") or ""),
                "soap_note": str(payload.get("soap_note") or ""),
                "approved_timestamp": str(payload.get("approved_timestamp") or ""),
            }, actor="import_local")
    return f"{count} of {len(files)} approved SOAP note(s) imported"


def import_access(dry_run: bool) -> str:
    path = SOURCES["access"]
    if not path.is_file():
        return "no local access_control.json (first sign-in will create the Super Admin)"
    current = access_store.load_store(force=True)
    if current["users"]:
        return f"skipped: Snowflake already has {len(current['users'])} user(s)"
    store = _read_json(path)
    if not dry_run:
        access_store.save_store(store)
    return f"{len(store.get('users', []))} user(s), {len(store.get('audit', []))} audit event(s) imported"


def import_ehr(dry_run: bool) -> str:
    if ehr_store.load_payload() is not None:
        return "skipped: EHR already seeded in Snowflake"
    path = SOURCES["ehr"]
    if not path.is_file():
        return "no local EHR JSON (the API seeds it from the workbook on first use)"
    payload = _read_json(path)
    if not dry_run:
        ehr_store.save_payload(payload)
    sheets = payload.get("sheets", {})
    rows = sum(len(sheet.get("records") or sheet.get("raw_rows") or []) for sheet in sheets.values())
    return f"{len(sheets)} sheet(s), {rows} row(s) imported"


def import_vbc(dry_run: bool) -> str:
    results = []
    for key in ("vbc_state", "vbc_poc_state"):
        path = SOURCES[key]
        if not path.is_file():
            continue
        if vbc_state.load(path.stem) is not None:
            results.append(f"{key}: already present")
            continue
        if not dry_run:
            # Store the exact file text so the audit hash chain stays verifiable.
            state = _read_json(path)
            client.execute(
                "INSERT INTO VBC.STATE_DOCUMENTS (DOC_KEY, REVISION, STATE_TEXT, STATE_JSON) "
                "SELECT %s, %s, %s, PARSE_JSON(%s)",
                (path.stem, int(state.get("revision", 0) or 0), path.read_text(encoding="utf-8"),
                 path.read_text(encoding="utf-8")),
            )
        results.append(f"{key}: imported")
    return ", ".join(results) or "no local VBC state files"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    steps = [
        ("EHR chart", import_ehr),
        ("Users & audit", import_access),
        ("Consultations", import_encounters),
        ("Approved SOAP notes", import_soap_notes),
        ("VBC state", import_vbc),
    ]
    try:
        for label, step in steps:
            print(f"{label:<22} {step(args.dry_run)}", flush=True)
    except client.StorageError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Dry run - nothing written." if args.dry_run else "Import complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
