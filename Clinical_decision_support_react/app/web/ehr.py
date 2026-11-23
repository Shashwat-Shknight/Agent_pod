"""In-process EHR access (the Streamlit app called the same service over HTTP on localhost)."""

from __future__ import annotations

import hashlib
import json
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


# API field -> Patient_Master column. Only these demographics are editable through the API.
PATIENT_FIELDS = {
    "first_name": "First_Name",
    "last_name": "Last_Name",
    "dob": "DOB",
    "sex": "Sex_at_Birth",
    "mobile": "Mobile",
    "email": "Email",
    "street_address": "Street_Address",
    "city": "City",
    "state": "State",
    "zip": "ZIP",
    "preferred_language": "Preferred_Language",
}


def patient_etag(patient: dict[str, Any]) -> str:
    """Fingerprint of the patient row: an edit or delete must name the state the user last saw."""
    text = json.dumps(patient, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def patient_view(patient: dict[str, Any]) -> dict[str, Any]:
    view = {field: str(patient.get(column) or "") for field, column in PATIENT_FIELDS.items()}
    view["dob"] = view["dob"][:10]
    name = patient.get("Legal_Name") or " ".join(p for p in (patient.get("First_Name"), patient.get("Last_Name")) if p)
    return {
        "patient_id": str(patient.get("Patient_ID") or ""),
        "mrn": str(patient.get("MRN") or ""),
        "name": str(name or "Unknown patient"),
        "status": str(patient.get("Record_Status") or "Active"),
        **view,
        "etag": patient_etag(patient),
    }


def get_patient(patient_id: str) -> dict[str, Any]:
    try:
        return ehr.get_patient_by_id(patient_id)
    except KeyError as error:
        raise ApiError(404, "patient_not_found", f"EHR patient {patient_id} was not found. It may have been removed.") from error


def find_duplicates(first_name: str, last_name: str, dob: str) -> list[dict[str, Any]]:
    key = (first_name.strip().casefold(), last_name.strip().casefold(), dob[:10])
    return [p for p in ehr.list_patients()
            if (str(p.get("First_Name") or "").strip().casefold(), str(p.get("Last_Name") or "").strip().casefold(),
                str(p.get("DOB") or "")[:10]) == key]


def create_patient(first_name: str, last_name: str, dob: str, sex: str, **extra: str) -> dict[str, Any]:
    values = {"First_Name": first_name, "Last_Name": last_name, "Legal_Name": f"{first_name} {last_name}",
              "DOB": dob, "Sex_at_Birth": sex}
    values.update({PATIENT_FIELDS[key]: value for key, value in extra.items() if key in PATIENT_FIELDS and value})
    try:
        patient = ehr.create_patient(values)
    except ValueError as error:
        raise ApiError(400, "invalid_patient", str(error)) from error
    invalidate()
    return patient


def update_patient(patient_id: str, etag: str, changes: dict[str, str]) -> dict[str, Any]:
    """Apply demographic changes if nobody else changed the patient since ``etag`` was read."""
    with ehr._STORE_LOCK:  # re-entrant: the check and the write happen as one step
        current = get_patient(patient_id)
        if etag != patient_etag(current):
            raise ApiError(409, "stale_record", "Someone else changed this patient since you opened it. Reload to see the latest details.")
        updates = {PATIENT_FIELDS[key]: value for key, value in changes.items() if key in PATIENT_FIELDS}
        if "First_Name" in updates or "Last_Name" in updates:
            first = updates.get("First_Name", current.get("First_Name") or "")
            last = updates.get("Last_Name", current.get("Last_Name") or "")
            updates["Legal_Name"] = f"{first} {last}".strip()
        patient = ehr.update_patient(patient_id, updates) if updates else current
    invalidate()
    return patient


def chart_row_count(patient_id: str) -> int:
    return sum(len(rows) for rows in chart(patient_id).get("records", {}).values())


def delete_patient(patient_id: str, etag: str) -> None:
    with ehr._STORE_LOCK:
        current = get_patient(patient_id)
        if etag != patient_etag(current):
            raise ApiError(409, "stale_record", "Someone else changed this patient since you opened it. Reload first.")
        ehr.delete_patient(patient_id)
    invalidate()


def cancel_consultation_links(record: dict[str, Any]) -> str | None:
    """A deleted consultation's EHR encounter becomes 'Cancelled' and its draft note 'Entered in Error' (kept, not erased)."""
    patient_id = record_patient_id(record)
    encounter_id, note_id = record.get("ehr_encounter_id"), record.get("ehr_note_id")
    if not patient_id or not (encounter_id or note_id):
        return None
    try:
        records = ehr.get_patient_chart(patient_id).get("records", {})
        for sheet, key, value, status in (("Encounters", "Encounter_ID", encounter_id, "Cancelled"),
                                          ("Clinical_Notes", "Note_ID", note_id, "Entered in Error")):
            rows = records.get(sheet, [])
            hit = False
            for row in rows:
                if value and str(row.get(key, "")) == str(value):
                    row["Status"] = status
                    hit = True
            if hit:
                ehr.replace_patient_records(patient_id, sheet, rows)
        invalidate()
        return None
    except KeyError:
        return None  # the patient itself is gone: nothing left to mark
    except Exception as error:  # noqa: BLE001 - the deletion stands; report the EHR problem
        logger.warning("EHR cancel failed for %s: %s", record.get("id"), error)
        return f"Deleted, but the linked EHR encounter could not be marked cancelled: {error}"


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
