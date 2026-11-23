"""CLINICAL.ENCOUNTERS repository (consultation records)."""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from app.db import client

# Last hash this process wrote/read per encounter: avoids a round trip for unchanged records
# when the UI calls save_db() with the whole list.
_KNOWN_HASH: dict[str, str] = {}
_LOCK = threading.Lock()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def list_encounters() -> list[dict[str, Any]]:
    rows = client.query(
        "SELECT ENCOUNTER_ID, RECORD_JSON FROM CLINICAL.ENCOUNTERS "
        "WHERE NOT IS_DELETED ORDER BY CREATED_AT DESC, ENCOUNTER_AT DESC NULLS LAST, ENCOUNTER_ID"
    )
    records = []
    with _LOCK:
        for row in rows:
            record = client.variant(row["RECORD_JSON"])
            if isinstance(record, dict):
                records.append(record)
                _KNOWN_HASH[str(row["ENCOUNTER_ID"])] = _hash(client.to_json(record))
    return records


def get_encounter(encounter_id: str) -> dict[str, Any] | None:
    rows = client.query(
        "SELECT RECORD_JSON FROM CLINICAL.ENCOUNTERS WHERE ENCOUNTER_ID = %s AND NOT IS_DELETED", (encounter_id,)
    )
    return client.variant(rows[0]["RECORD_JSON"]) if rows else None


def save_encounter(record: dict[str, Any], actor: str = "") -> str:
    """Upsert one record. Returns 'saved', 'unchanged' or raises StorageError."""
    encounter_id = str(record.get("id") or "").strip()
    if not encounter_id:
        raise client.StorageError("Encounter record has no id")
    text = client.to_json(record)
    digest = _hash(text)
    with _LOCK:
        if _KNOWN_HASH.get(encounter_id) == digest:
            return "unchanged"
    rows = client.query("CALL CLINICAL.UPSERT_ENCOUNTER(%s, %s)", (text, actor or "system"))
    result = client.variant(next(iter(rows[0].values()))) if rows else {}
    if isinstance(result, dict) and result.get("status") == "error":
        raise client.StorageError(f"Encounter {encounter_id} was not saved: {result.get('message')}")
    with _LOCK:
        _KNOWN_HASH[encounter_id] = digest
    return str(result.get("status", "saved")) if isinstance(result, dict) else "saved"


def save_encounters(records: list[dict[str, Any]], actor: str = "") -> dict[str, int]:
    summary = {"saved": 0, "unchanged": 0}
    for record in records:
        if isinstance(record, dict) and record.get("id"):
            status = save_encounter(record, actor)
            summary["saved" if status == "saved" else "unchanged"] += 1
    return summary


def soft_delete(encounter_id: str, actor: str = "") -> bool:
    count = client.execute(
        "UPDATE CLINICAL.ENCOUNTERS SET IS_DELETED = TRUE, UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = %s "
        "WHERE ENCOUNTER_ID = %s AND NOT IS_DELETED",
        (actor or "system", encounter_id),
    )
    with _LOCK:
        _KNOWN_HASH.pop(encounter_id, None)
    return count > 0


def history(encounter_id: str) -> list[dict[str, Any]]:
    rows = client.query(
        "SELECT ROW_VERSION, CHANGED_BY, CHANGED_AT FROM CLINICAL.ENCOUNTER_HISTORY "
        "WHERE ENCOUNTER_ID = %s ORDER BY ROW_VERSION DESC",
        (encounter_id,),
    )
    return [{"version": row["ROW_VERSION"], "by": row["CHANGED_BY"], "at": row["CHANGED_AT"]} for row in rows]
