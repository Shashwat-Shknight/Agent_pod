"""Consultation records with optimistic concurrency.

Records keep the exact shape the Streamlit app used (same Snowflake rows), plus an integer "version".
Every write re-reads the record, checks the version the client last saw, applies the change, bumps the
version and saves - so two people working on one encounter can never silently overwrite each other.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.db import encounters as encounter_repo
from app.db.settings import use_snowflake
from app.domain.settings import DB_FILE
from app.web.errors import ApiError

_LOCK = threading.RLock()  # serialises read-modify-write cycles inside this process
_RECORD_LOCKS: dict[str, threading.Lock] = {}


def _record_lock(record_id: str) -> threading.Lock:
    with _LOCK:
        return _RECORD_LOCKS.setdefault(record_id, threading.Lock())


def _read_file() -> list[dict[str, Any]]:
    if not DB_FILE.exists():
        return []
    try:
        payload = json.loads(DB_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload.get("records", []) if isinstance(payload, dict) else []


def _write_file(records: list[dict[str, Any]]) -> None:
    temp = DB_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"records": records}, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(DB_FILE)


def list_records() -> list[dict[str, Any]]:
    if use_snowflake():
        return encounter_repo.list_encounters()
    with _LOCK:
        return _read_file()


def get_record(record_id: str) -> dict[str, Any]:
    record = None
    if use_snowflake():
        record = encounter_repo.get_encounter(record_id)
    else:
        with _LOCK:
            record = next((r for r in _read_file() if r.get("id") == record_id), None)
    if not isinstance(record, dict):
        raise ApiError(404, "not_found", f"Consultation {record_id} was not found. It may have been removed.")
    return record


def version_of(record: dict[str, Any]) -> int:
    try:
        return int(record.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _save(record: dict[str, Any], actor: str) -> None:
    if use_snowflake():
        encounter_repo.save_encounter(record, actor)
        return
    with _LOCK:
        records = _read_file()
        for index, existing in enumerate(records):
            if existing.get("id") == record.get("id"):
                records[index] = record
                break
        else:
            records.insert(0, record)
        _write_file(records)


def insert_record(record: dict[str, Any], actor: str) -> dict[str, Any]:
    record["version"] = 1
    _save(record, actor)
    return record


@contextmanager
def edit_record(record_id: str, actor: str, expected_version: int | None) -> Iterator[dict[str, Any]]:
    """Read-check-modify-save. Raise inside the block to abort without saving.

    expected_version None skips the check (server-initiated changes); otherwise a mismatch is a 409.
    """
    with _record_lock(record_id):
        record = get_record(record_id)
        current = version_of(record)
        if expected_version is not None and expected_version != current:
            raise ApiError(
                409,
                "stale_record",
                "This consultation was changed by someone else since you opened it. Reload to see the latest version.",
                {"current_version": current},
            )
        yield record
        record["version"] = current + 1
        _save(record, actor)


def mutate(record_id: str, actor: str, expected_version: int | None, change: Callable[[dict[str, Any]], Any]) -> Any:
    with edit_record(record_id, actor, expected_version) as record:
        return change(record)
