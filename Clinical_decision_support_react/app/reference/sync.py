"""Synchronise official code sets into Snowflake (REFERENCE schema).

    .venv\\Scripts\\python -m app.reference.sync                 ICD-10-CM + HCPCS
    .venv\\Scripts\\python -m app.reference.sync --system ICD10CM
    .venv\\Scripts\\python -m app.reference.sync --status

ICD-10-CM: every CDC release newer than the latest loaded one is applied in effective-date
order (first run loads the last INITIAL_ICD_RELEASES releases so history is available).
HCPCS: a fresh snapshot is applied when the last one is older than HCPCS_REFRESH_DAYS.

Each release is applied in one transaction as a type-2 history change:
  * new code      -> new row, VALID_FROM = release effective date (or the source's add date)
  * revised code  -> previous row closed (VALID_TO = effective date - 1), new row inserted
  * deleted code  -> latest row closed, RETIRED_IN_RELEASE set
Re-running is safe: an already-loaded release (same checksum) is skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import date, timedelta
from typing import Any

import requests

from app.db import client
from app.reference import sources

log = logging.getLogger("nuucare.reference")

INITIAL_ICD_RELEASES = 3
HCPCS_REFRESH_DAYS = 7
STAGE_CHUNK = 1500
SYSTEMS = ("ICD10CM", "HCPCS")


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------
def _start_run(code_system: str, trigger: str) -> str | None:
    """Returns a run id, or None when another sync of this system is already running."""
    running = client.query(
        "SELECT RUN_ID FROM REFERENCE.SYNC_RUNS WHERE CODE_SYSTEM = %s AND STATUS = 'RUNNING' "
        "AND STARTED_AT > DATEADD(hour, -2, CURRENT_TIMESTAMP())",
        (code_system,),
    )
    if running:
        return None
    run_id = str(uuid.uuid4())
    client.execute(
        "INSERT INTO REFERENCE.SYNC_RUNS (RUN_ID, CODE_SYSTEM, TRIGGERED_BY, STATUS) VALUES (%s, %s, %s, 'RUNNING')",
        (run_id, code_system, trigger),
    )
    return run_id


def _finish_run(run_id: str, status: str, message: str, release_id: str | None = None, detail: Any = None) -> None:
    client.execute(
        "UPDATE REFERENCE.SYNC_RUNS SET STATUS = %s, MESSAGE = %s, RELEASE_ID = %s, DETAIL = PARSE_JSON(%s), "
        "FINISHED_AT = CURRENT_TIMESTAMP() WHERE RUN_ID = %s",
        (status, message[:4000], release_id, client.to_json(detail or {}), run_id),
    )


def loaded_releases(code_system: str) -> list[dict[str, Any]]:
    return client.query(
        "SELECT RELEASE_ID, EFFECTIVE_FROM, CHECKSUM, LOADED_AT FROM REFERENCE.CODE_SET_RELEASES "
        "WHERE CODE_SYSTEM = %s ORDER BY EFFECTIVE_FROM",
        (code_system,),
    )


# ---------------------------------------------------------------------------
# Applying a release
# ---------------------------------------------------------------------------
def _stage(run_id: str, release: sources.Release) -> None:
    rows = [
        {
            "c": row.code, "r": row.code_raw, "s": row.short_description, "l": row.long_description,
            "b": row.is_billable,
            "f": row.source_valid_from.isoformat() if row.source_valid_from else None,
            "t": row.source_valid_to.isoformat() if row.source_valid_to else None,
            "h": row.row_hash(),
        }
        for row in release.rows
    ]
    for start in range(0, len(rows), STAGE_CHUNK):
        client.execute(
            "INSERT INTO REFERENCE.CODES_STAGE (RUN_ID, CODE_SYSTEM, CODE, CODE_RAW, SHORT_DESCRIPTION, "
            "LONG_DESCRIPTION, IS_BILLABLE, SOURCE_VALID_FROM, SOURCE_VALID_TO, ROW_HASH) "
            "SELECT %s, %s, v.value:c::STRING, v.value:r::STRING, v.value:s::STRING, v.value:l::STRING, "
            "v.value:b::BOOLEAN, v.value:f::DATE, v.value:t::DATE, v.value:h::STRING "
            "FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s))) v",
            (run_id, release.code_system, json.dumps(rows[start:start + STAGE_CHUNK], ensure_ascii=False)),
        )


def apply_release(release: sources.Release, run_id: str, actor: str = "sync") -> dict[str, int]:
    system, rel, effective = release.code_system, release.release_id, release.effective_from
    closing = (effective - timedelta(days=1)).isoformat()
    params = {"run": run_id, "sys": system, "rel": rel, "eff": effective.isoformat(), "close": closing}
    client.execute("DELETE FROM REFERENCE.CODES_STAGE WHERE RUN_ID = %(run)s", params)
    _stage(run_id, release)
    try:
        with client.transaction() as cursor:
            def scalar(sql: str) -> int:
                cursor.execute(sql, params)
                return int(cursor.fetchone()[0] or 0)

            latest = ("SELECT * FROM REFERENCE.CODES WHERE CODE_SYSTEM = %(sys)s AND IS_LATEST")
            added = scalar(
                f"SELECT COUNT(*) FROM REFERENCE.CODES_STAGE s WHERE s.RUN_ID = %(run)s AND NOT EXISTS "
                f"(SELECT 1 FROM ({latest}) c WHERE c.CODE_RAW = s.CODE_RAW)"
            )
            changed = scalar(
                f"SELECT COUNT(*) FROM REFERENCE.CODES_STAGE s JOIN ({latest}) c ON c.CODE_RAW = s.CODE_RAW "
                f"WHERE s.RUN_ID = %(run)s AND c.ROW_HASH <> s.ROW_HASH"
            )
            retired = scalar(
                f"SELECT COUNT(*) FROM ({latest}) c WHERE c.VALID_TO IS NULL AND NOT EXISTS "
                f"(SELECT 1 FROM REFERENCE.CODES_STAGE s WHERE s.RUN_ID = %(run)s AND s.CODE_RAW = c.CODE_RAW)"
            )
            # 1. revised (or deleted-then-re-added) codes: close the previous version
            cursor.execute(
                "UPDATE REFERENCE.CODES c SET IS_LATEST = FALSE, "
                "  VALID_TO = LEAST(COALESCE(c.VALID_TO, %(close)s::DATE), %(close)s::DATE) "
                "FROM REFERENCE.CODES_STAGE s "
                "WHERE s.RUN_ID = %(run)s AND c.CODE_SYSTEM = %(sys)s AND c.IS_LATEST "
                "  AND c.CODE_RAW = s.CODE_RAW AND (c.ROW_HASH <> s.ROW_HASH OR c.RETIRED_IN_RELEASE IS NOT NULL)",
                params,
            )
            # 2. deleted codes: close the latest version
            cursor.execute(
                "UPDATE REFERENCE.CODES c SET VALID_TO = %(close)s::DATE, RETIRED_IN_RELEASE = %(rel)s "
                "WHERE c.CODE_SYSTEM = %(sys)s AND c.IS_LATEST AND c.VALID_TO IS NULL AND NOT EXISTS ("
                "  SELECT 1 FROM REFERENCE.CODES_STAGE s WHERE s.RUN_ID = %(run)s AND s.CODE_RAW = c.CODE_RAW)",
                params,
            )
            # 3. new and revised codes: insert the new version
            cursor.execute(
                "INSERT INTO REFERENCE.CODES (CODE_SYSTEM, CODE, CODE_RAW, SHORT_DESCRIPTION, LONG_DESCRIPTION, "
                "  IS_BILLABLE, VALID_FROM, VALID_TO, IS_LATEST, RELEASE_ID, ROW_HASH) "
                "SELECT s.CODE_SYSTEM, s.CODE, s.CODE_RAW, s.SHORT_DESCRIPTION, s.LONG_DESCRIPTION, s.IS_BILLABLE, "
                "  IFF(EXISTS (SELECT 1 FROM REFERENCE.CODES p WHERE p.CODE_SYSTEM = %(sys)s AND p.CODE_RAW = s.CODE_RAW), "
                "      %(eff)s::DATE, COALESCE(s.SOURCE_VALID_FROM, %(eff)s::DATE)), "
                "  s.SOURCE_VALID_TO, TRUE, %(rel)s, s.ROW_HASH "
                "FROM REFERENCE.CODES_STAGE s "
                "WHERE s.RUN_ID = %(run)s AND NOT EXISTS ("
                "  SELECT 1 FROM REFERENCE.CODES c WHERE c.CODE_SYSTEM = %(sys)s AND c.IS_LATEST AND c.CODE_RAW = s.CODE_RAW)",
                params,
            )
            cursor.execute(
                "INSERT INTO REFERENCE.CODE_SET_RELEASES (CODE_SYSTEM, RELEASE_ID, SOURCE, SOURCE_URL, EFFECTIVE_FROM, "
                "  CODE_COUNT, BILLABLE_COUNT, ADDED_COUNT, CHANGED_COUNT, RETIRED_COUNT, CHECKSUM, LOADED_BY) "
                "VALUES (%(sys)s, %(rel)s, %(src)s, %(url)s, %(eff)s, %(n)s, %(nb)s, %(a)s, %(c)s, %(r)s, %(sum)s, %(by)s)",
                {**params, "src": release.source, "url": release.source_url, "n": len(release.rows),
                 "nb": sum(row.is_billable for row in release.rows), "a": added, "c": changed, "r": retired,
                 "sum": release.checksum, "by": actor},
            )
    finally:
        client.execute("DELETE FROM REFERENCE.CODES_STAGE WHERE RUN_ID = %(run)s", params)
    return {"codes": len(release.rows), "added": added, "changed": changed, "retired": retired}


# ---------------------------------------------------------------------------
# Per-system sync
# ---------------------------------------------------------------------------
def _offline_fallback(system: str, run_id: str, trigger: str, done: list, error: Exception) -> dict[str, Any]:
    """Live source unreachable (e.g. blocked by a corporate network).

    If this code system has never been loaded, load the bundled snapshot (data/reference/) so the app is
    usable immediately; otherwise keep the loaded release. Either way the run is recorded.
    """
    from app.reference import snapshot

    reason = f"live source unreachable ({type(error).__name__})"
    if done:
        message = f"Kept {done[-1]['RELEASE_ID']}: {reason}"
        _finish_run(run_id, "UP_TO_DATE", message, done[-1]["RELEASE_ID"])
        return {"system": system, "status": "OFFLINE", "message": message}
    if not snapshot.available(system):
        _finish_run(run_id, "FAILED", f"{reason}; no bundled snapshot")
        raise error
    release = snapshot.load(system)
    stats = apply_release(release, run_id, actor=f"{trigger}:snapshot")
    message = f"Loaded bundled snapshot {release.release_id}: {reason}. Live sync will update it when reachable."
    _finish_run(run_id, "SUCCEEDED", message, release.release_id, stats)
    return {"system": system, "status": "SUCCEEDED", "message": message,
            "applied": [{"release": release.release_id, "effective_from": release.effective_from.isoformat(), **stats}]}


def sync_icd10cm(trigger: str = "cli") -> dict[str, Any]:
    run_id = _start_run("ICD10CM", trigger)
    if run_id is None:
        return {"system": "ICD10CM", "status": "SKIPPED", "message": "another sync is running"}
    try:
        done = loaded_releases("ICD10CM")
        latest = max((row["EFFECTIVE_FROM"] for row in done), default=None)
        try:
            published = sources.discover_icd10cm_releases()
        except requests.RequestException as error:
            return _offline_fallback("ICD10CM", run_id, trigger, done, error)
        pending = [ref for ref in published if latest is None or ref.effective_from > latest]
        if latest is None:
            pending = pending[-INITIAL_ICD_RELEASES:]
        # A release only takes effect on its date; never activate a future one early in the "current" view —
        # VALID_FROM handles that, so it is safe (and useful) to load it as soon as CDC publishes it.
        applied, skipped = [], []
        for ref in pending:
            try:
                release = sources.fetch_icd10cm_release(ref)
            except LookupError as error:  # e.g. an update folder with only addenda / PDFs
                skipped.append(f"{ref.release_id}: {error}")
                log.warning("ICD-10-CM %s skipped: %s", ref.release_id, error)
                continue
            stats = apply_release(release, run_id, actor=trigger)
            applied.append({"release": ref.release_id, "effective_from": ref.effective_from.isoformat(), **stats})
            log.info("ICD-10-CM %s applied: %s", ref.release_id, stats)
        newest = published[-1].release_id if published else None
        if applied:
            message = "Applied " + ", ".join(item["release"] for item in applied)
            _finish_run(run_id, "SUCCEEDED", message, applied[-1]["release"], {"applied": applied, "skipped": skipped})
            return {"system": "ICD10CM", "status": "SUCCEEDED", "message": message, "applied": applied}
        message = f"Up to date (latest published: {newest})"
        _finish_run(run_id, "UP_TO_DATE", message, newest)
        return {"system": "ICD10CM", "status": "UP_TO_DATE", "message": message}
    except Exception as error:  # noqa: BLE001 - recorded, then re-raised
        _finish_run(run_id, "FAILED", f"{type(error).__name__}: {error}")
        raise


def sync_hcpcs(trigger: str = "cli", force: bool = False) -> dict[str, Any]:
    done = loaded_releases("HCPCS")
    if done and not force and done[-1]["EFFECTIVE_FROM"] > date.today() - timedelta(days=HCPCS_REFRESH_DAYS):
        return {"system": "HCPCS", "status": "UP_TO_DATE",
                "message": f"Snapshot {done[-1]['RELEASE_ID']} is newer than {HCPCS_REFRESH_DAYS} days"}
    run_id = _start_run("HCPCS", trigger)
    if run_id is None:
        return {"system": "HCPCS", "status": "SKIPPED", "message": "another sync is running"}
    try:
        try:
            release = sources.fetch_hcpcs_release()
        except requests.RequestException as error:
            return _offline_fallback("HCPCS", run_id, trigger, done, error)
        if done and done[-1]["CHECKSUM"] == release.checksum:
            message = f"No HCPCS changes since {done[-1]['RELEASE_ID']}"
            _finish_run(run_id, "UP_TO_DATE", message, done[-1]["RELEASE_ID"])
            return {"system": "HCPCS", "status": "UP_TO_DATE", "message": message}
        if any(row["RELEASE_ID"] == release.release_id for row in done):
            release.release_id += f"-{uuid.uuid4().hex[:4]}"
        stats = apply_release(release, run_id, actor=trigger)
        message = f"Applied {release.release_id}"
        _finish_run(run_id, "SUCCEEDED", message, release.release_id, stats)
        return {"system": "HCPCS", "status": "SUCCEEDED", "message": message, **stats}
    except Exception as error:  # noqa: BLE001
        _finish_run(run_id, "FAILED", f"{type(error).__name__}: {error}")
        raise


def sync_all(trigger: str = "cli", systems: tuple[str, ...] = SYSTEMS, force: bool = False) -> list[dict[str, Any]]:
    results = []
    for system in systems:
        try:
            results.append(sync_icd10cm(trigger) if system == "ICD10CM" else sync_hcpcs(trigger, force=force))
        except Exception as error:  # noqa: BLE001 - one system failing must not stop the other
            log.exception("Code-set sync failed for %s", system)
            results.append({"system": system, "status": "FAILED", "message": f"{type(error).__name__}: {error}"})
    if any(result.get("status") == "SUCCEEDED" for result in results):
        from app.reference import lookup

        lookup.clear_cache()
    return results


def status() -> list[dict[str, Any]]:
    return client.query("SELECT * FROM REFERENCE.V_CODE_SET_STATUS ORDER BY CODE_SYSTEM")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("snowflake").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--system", choices=[*SYSTEMS, "all"], default="all")
    parser.add_argument("--status", action="store_true", help="show loaded code sets")
    parser.add_argument("--force", action="store_true", help="refresh HCPCS even if the snapshot is recent")
    args = parser.parse_args(argv)
    try:
        if not args.status:
            systems = SYSTEMS if args.system == "all" else (args.system,)
            for result in sync_all("cli", systems, force=args.force):
                print(f"{result['system']:<8} {result['status']:<11} {result.get('message', '')}")
                for item in result.get("applied", []):
                    print(f"           {item['release']:<12} effective {item['effective_from']}: {item['codes']} codes, "
                          f"+{item['added']} added, ~{item['changed']} revised, -{item['retired']} deleted")
        for row in status():
            print(f"{row['CODE_SYSTEM']:<8} active {row['ACTIVE_RELEASE']:<16} effective {row['EFFECTIVE_FROM']}  "
                  f"in force: {row['CODES_IN_FORCE']} ({row['BILLABLE_IN_FORCE']} billable)  source: {row['SOURCE']}")
    except client.StorageError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
