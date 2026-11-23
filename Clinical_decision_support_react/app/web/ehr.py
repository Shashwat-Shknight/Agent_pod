"""In-process EHR access (the Streamlit app called the same service over HTTP on localhost)."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from app.services import ehr_json_service as ehr
from app.web.errors import ApiError

logger = logging.getLogger("nuucare.ehr")

_CACHE_SECONDS = 30
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, loader):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def invalidate() -> None:
    with _cache_lock:
        _cache.clear()


def patients() -> list[dict[str, Any]]:
    return _cached("patients", ehr.list_patients)


def export() -> dict[str, Any]:
    return _cached("export", lambda: ehr.export_patient_data(None))


def chart(patient_id: str | None) -> dict[str, Any]:
    """The patient's chart, or {} when the record is not linked or the patient no longer exists."""
    if not patient_id:
        return {}
    try:
        return _cached(f"chart:{patient_id}", lambda: ehr.get_patient_chart(str(patient_id)))
    except KeyError:
        return {}


def record_patient_id(record: dict[str, Any]) -> str:
    return str(record.get("ehr_patient_id") or record.get("patient_id") or "")


def create_patient(first_name: str, last_name: str, dob: str, sex: str) -> dict[str, Any]:
    try:
        patient = ehr.create_patient({"First_Name": first_name, "Last_Name": last_name, "DOB": dob, "Sex_at_Birth": sex})
    except ValueError as error:
        raise ApiError(400, "invalid_patient", str(error)) from error
    invalidate()
    return patient


def register_consultation(record: dict[str, Any], encounter_type: str) -> None:
    """Link the consultation to an EHR encounter and a draft SOAP note (same rows the Streamlit app created)."""
    patient_id = record_patient_id(record)
    if not patient_id:
        raise ApiError(400, "patient_required", "Choose an EHR patient or register a new one first.")
    try:
        patient = ehr.get_patient_by_id(patient_id)
    except KeyError as error:
        raise ApiError(404, "patient_not_found", f"EHR patient {patient_id} was not found.") from error
    record["ehr_patient_id"] = patient_id
    record["patient_id"] = patient_id
    record["mrn"] = patient.get("MRN", record.get("mrn", ""))
    record["dob"] = patient.get("DOB", record.get("dob", ""))
    try:
        service_date = datetime.strptime(record.get("date", ""), "%d/%m/%Y").date().isoformat()
    except (TypeError, ValueError):
        service_date = datetime.now().date().isoformat()
    if not record.get("ehr_encounter_id"):
        encounter = ehr.add_patient_record(patient_id, "Encounters", {
            "Encounter_ID": f"ENC-{uuid.uuid4().hex[:10].upper()}",
            "Patient_ID": patient_id,
            "Date": service_date,
            "Type": encounter_type,
            "Department": "Primary Care",
            "Provider": record.get("doctor", "Unassigned"),
            "Facility": "NuuCare Demo",
            "Status": "In Progress",
            "Primary_Diagnosis": record.get("patient_summary", ""),
            "Priority": "Routine",
        })
        record["ehr_encounter_id"] = encounter.get("Encounter_ID")
    if not record.get("ehr_note_id"):
        note = ehr.add_patient_record(patient_id, "Clinical_Notes", {
            "Note_ID": f"NOTE-{uuid.uuid4().hex[:10].upper()}",
            "Patient_ID": patient_id,
            "Encounter_ID": record["ehr_encounter_id"],
            "Note_Type": "SOAP",
            "Service_Date": service_date,
            "Author": record.get("doctor", "Unassigned"),
            "Note_Summary": record.get("summary") or record.get("patient_summary", ""),
            "Status": "Draft",
        })
        record["ehr_note_id"] = note.get("Note_ID")
    invalidate()


def sync_clinical_note(record: dict[str, Any], approved: bool = False) -> str | None:
    """Mirror the SOAP text into the EHR note. Returns a warning string instead of failing the user's save."""
    patient_id, note_id = record_patient_id(record), record.get("ehr_note_id")
    if not patient_id or not note_id:
        return None
    try:
        notes = ehr.get_patient_chart(patient_id).get("records", {}).get("Clinical_Notes", [])
        for note in notes:
            if str(note.get("Note_ID", "")) == str(note_id):
                note["Note_Summary"] = record.get("summary", "")
                note["Status"] = "Signed" if approved else "Draft"
                break
        else:
            return "The EHR note linked to this consultation was not found; the consultation itself was saved."
        ehr.replace_patient_records(patient_id, "Clinical_Notes", notes)
        invalidate()
        return None
    except Exception as error:  # noqa: BLE001 - the clinical save already succeeded; report, don't fail
        logger.warning("EHR note sync failed for %s: %s", record.get("id"), error)
        return f"Saved, but the EHR note could not be updated: {error}"
