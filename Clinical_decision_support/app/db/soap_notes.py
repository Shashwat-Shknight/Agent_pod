"""CLINICAL.SOAP_NOTES repository - append-only, versioned approvals."""

from __future__ import annotations

from typing import Any

from app.db import client

_LATEST = (
    "SELECT ENCOUNTER_ID, PATIENT_ID, STATUS, SOAP_NOTE, APPROVED_TIMESTAMP, VERSION "
    "FROM CLINICAL.SOAP_NOTES WHERE {where} "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY ENCOUNTER_ID ORDER BY VERSION DESC) = 1"
)


def _payload(row: dict[str, Any]) -> dict[str, str]:
    return {
        "encounter_id": str(row["ENCOUNTER_ID"]),
        "patient_id": str(row["PATIENT_ID"] or ""),
        "status": str(row["STATUS"]),
        "approved_timestamp": str(row["APPROVED_TIMESTAMP"] or ""),
        "soap_note": str(row["SOAP_NOTE"] or ""),
    }


def _append(encounter_id: str, patient_id: str, status: str, soap_note: str, approved_timestamp: str, actor: str) -> None:
    client.execute(
        "INSERT INTO CLINICAL.SOAP_NOTES "
        "(ENCOUNTER_ID, VERSION, PATIENT_ID, STATUS, SOAP_NOTE, APPROVED_TIMESTAMP, ACTOR) "
        "SELECT %s, COALESCE(MAX(VERSION), 0) + 1, %s, %s, %s, %s, %s "
        "FROM CLINICAL.SOAP_NOTES WHERE ENCOUNTER_ID = %s",
        (encounter_id, patient_id, status, soap_note, approved_timestamp, actor or "system", encounter_id),
    )


def save_approved(payload: dict[str, str], actor: str = "") -> None:
    _append(
        payload["encounter_id"], payload.get("patient_id", ""), "approved",
        payload["soap_note"], payload.get("approved_timestamp", ""), actor,
    )


def load_latest(encounter_id: str) -> dict[str, str] | None:
    rows = client.query(_LATEST.format(where="ENCOUNTER_ID = %s"), (str(encounter_id),))
    return _payload(rows[0]) if rows else None


def load_approved(encounter_id: str) -> dict[str, str] | None:
    latest = load_latest(encounter_id)
    return latest if latest and latest["status"] == "approved" else None


def withdraw(encounter_id: str, actor: str = "") -> bool:
    latest = load_latest(encounter_id)
    if not latest or latest["status"] != "approved":
        return False
    _append(str(encounter_id), latest["patient_id"], "withdrawn", latest["soap_note"], "", actor)
    return True


def list_approved() -> list[dict[str, str]]:
    rows = client.query(_LATEST.format(where="TRUE"))
    return [_payload(row) for row in rows if row["STATUS"] == "approved"]
