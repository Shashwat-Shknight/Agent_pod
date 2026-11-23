"""Oversight dashboard: every patient across documentation, coding and billing.

`build_admin_workflow_rows` is pure (no Streamlit) so it can be unit-tested; the page
renders its output with the shared UI components.
"""

from datetime import datetime, timedelta
from html import escape






STAGES = ("Ambient Scribe", "Clinical Review", "Coding", "Billed", "EHR record")
STAGE_TONES = {"Ambient Scribe": "gray", "Clinical Review": "amber", "Coding": "violet", "Billed": "green", "EHR record": "blue"}
PENDING_ITEMS = {
    "Ambient Scribe": "SOAP draft not generated yet",
    "Clinical Review": "Clinical note awaiting sign-off",
    "Coding": "Codes awaiting validation and billing",
    "Billed": "Claim draft sent to RCM",
    "EHR record": "No consultation in this workspace",
}
DATE_WINDOWS = ("All time", "Today", "Last 7 days", "Last 30 days")
QUEUE_PAGE_SIZE = 12


def _text(value, fallback=""):
    text = str(value or "").strip()
    return text or fallback


def _parse_datetime(date_value, time_value=""):
    date_text, time_text = _text(date_value), _text(time_value) or "00:00"
    for pattern in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{date_text[:10]} {time_text}", pattern)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(date_text)
    except ValueError:
        return None


def _consultation_stage(record):
    if record.get("bill_generated_at"):
        return "Billed"
    if record.get("status") == "Approved":
        return "Coding"
    if record.get("summary") or record.get("patient_summary"):
        return "Clinical Review"
    return "Ambient Scribe"


def _patient_name(patient):
    """Legal name, else first + last name (patients registered from the app have no legal name)."""
    full_name = " ".join(part for part in (_text(patient.get("First_Name")), _text(patient.get("Last_Name"))) if part)
    return _text(patient.get("Legal_Name"), full_name or "Unknown patient")


def _sheet_records(payload, sheet):
    return ((payload or {}).get("sheets", {}).get(sheet) or {}).get("records") or []


def build_admin_workflow_rows(local_records, ehr_payload, user_names):
    """One row per patient: consultations first, then EHR patients without a consultation.

    `ehr_payload` is the EHR export ({"sheets": {"Patient_Master": {"records": [...]}, ...}});
    `user_names` maps usernames to display names for the owner column.
    """
    patients = {
        _text(row.get("Patient_ID")): row for row in _sheet_records(ehr_payload, "Patient_Master") if row.get("Patient_ID")
    }
    latest_encounters = {}
    for encounter in _sheet_records(ehr_payload, "Encounters"):
        patient_id = _text(encounter.get("Patient_ID"))
        current = latest_encounters.get(patient_id)
        if current is None or _text(encounter.get("Date")) > _text(current.get("Date")):
            latest_encounters[patient_id] = encounter

    rows, linked = [], set()
    for record in local_records or []:
        patient_id = _text(record.get("ehr_patient_id") or record.get("patient_id"))
        patient = patients.get(patient_id, {})
        linked.add(patient_id)
        stage = _consultation_stage(record)
        assignee = record.get("assigned_to")
        rows.append(
            {
                "Patient": _text(record.get("name"), _patient_name(patient)),
                "MRN": _text(record.get("mrn") or patient.get("MRN"), "Not recorded"),
                "Patient ID": patient_id or "Local only",
                "Encounter": _text(record.get("ehr_encounter_id") or record.get("id")),
                "Workflow": "IPD journey"
                if _text(record.get("ehr_encounter_type")).lower().startswith("inp")
                else "Consultation",
                "Stage": stage,
                "Owner": (user_names or {}).get(assignee, assignee) if assignee else "Unassigned",
                "Updated": _parse_datetime(record.get("approval_date") or record.get("date"), record.get("time")),
                "Pending item": PENDING_ITEMS[stage],
                "Priority": "Needs attention"
                if (not assignee and stage != "Billed") or record.get("rerouted_from")
                else ("Routine" if stage == "Billed" else "In progress"),
                "_origin": "consultation",
                "_record": record,
            }
        )
    for patient_id, patient in patients.items():
        if patient_id in linked:
            continue
        encounter = latest_encounters.get(patient_id, {})
        rows.append(
            {
                "Patient": _patient_name(patient),
                "MRN": _text(patient.get("MRN"), "Not recorded"),
                "Patient ID": patient_id,
                "Encounter": _text(encounter.get("Encounter_ID"), "—"),
                "Workflow": _text(encounter.get("Type"), "EHR record"),
                "Stage": "EHR record",
                "Owner": _text(encounter.get("Provider"), "—"),
                "Updated": _parse_datetime(encounter.get("Date")),
                "Pending item": PENDING_ITEMS["EHR record"],
                "Priority": "Routine",
                "_origin": "ehr",
                "_record": None,
            }
        )
    rows.sort(key=lambda row: row["Updated"] or datetime.min, reverse=True)
    return rows


def _filter_rows(rows, query, workflow, stage, window, now):
    matches = []
    for row in rows:
        haystack = " ".join((row["Patient"], row["MRN"], row["Patient ID"], row["Encounter"])).lower()
        if query and query not in haystack:
            continue
        if workflow != "All workflows" and row["Workflow"] != workflow:
            continue
        if stage != "All stages" and row["Stage"] != stage:
            continue
        if window != "All time":
            updated = row["Updated"]
            if updated is None:
                continue
            if window == "Today" and updated.date() != now.date():
                continue
            if window == "Last 7 days" and not timedelta(0) <= now - updated <= timedelta(days=7):
                continue
            if window == "Last 30 days" and not timedelta(0) <= now - updated <= timedelta(days=30):
                continue
        matches.append(row)
    return matches



def _row_key(row):
    return f"{row['_origin']}:{row['Patient ID']}:{row['Encounter']}"



