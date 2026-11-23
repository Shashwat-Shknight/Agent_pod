"""Consultations: patient selection, ambient scribe, CDS review, SOAP note and signature."""

from __future__ import annotations

import io
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field, field_validator

from app.agent.context import EncounterContext
from app.domain import access_control as ac
from app.domain.approved_soap import missing_soap_sections, save_approved_soap_note
from app.domain.clinical_text import (
    append_selected_insights_to_note,
    append_to_plan,
    get_insight_category,
    guideline_passages,
    parse_transcript_entries,
    recommendation_items,
    split_cds_sections,
)
from app.domain.ehr_chart import active_drug_allergies, care_gaps, chart_context, medication_safety_checks, primary_coverage
from app.domain.encounter_pipeline import MASTER_INSIGHTS, get_record_insights
from app.domain.masking import IDENTITY, NARRATIVE, field_class
from app.domain.settings import ANALYSIS_VERSION, APPROVED_SOAP_DIR
from app.web import ehr, records
from app.web.context import Caller, requires, store_transaction
from app.web.errors import ApiError

logger = logging.getLogger("nuucare.clinical")
router = APIRouter(prefix="/api", tags=["clinical"])

MIN_TRANSCRIPT_CHARS = 40
MAX_TRANSCRIPT_CHARS = 60_000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
ENCOUNTER_TYPES = ("Office Visit", "Outpatient", "Telehealth", "Follow-up", "Inpatient")
CODING_FIELDS = ("code_results", "code_decisions", "coding_audit", "cpt_results_history", "imported_codes",
                 "billing_overrides", "bill_estimate", "rcm_exceptions")


# ---------------------------------------------------------------- shared helpers
def log_history(record: dict[str, Any], action: str) -> None:
    record.setdefault("review_history", []).append({"at": datetime.now().strftime("%d %b %Y %H:%M"), "action": action})


def stage_of(record: dict[str, Any]) -> str:
    if record.get("bill_generated_at"):
        return "Billed"
    if record.get("status") == "Approved":
        return "Coding"
    if record.get("summary") or record.get("patient_summary"):
        return "Clinical Review"
    return "Ambient Scribe"


def record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "version": records.version_of(record),
        "name": record.get("name", ""),
        "mrn": record.get("mrn", ""),
        "patient_id": ehr.record_patient_id(record),
        "encounter_id": record.get("ehr_encounter_id") or "",
        "doctor": record.get("doctor", ""),
        "date": record.get("date", ""),
        "time": record.get("time", ""),
        "status": record.get("status", "Pending"),
        "approval_date": record.get("approval_date", ""),
        "assigned_to": record.get("assigned_to"),
        "coding_status": record.get("coding_status", ""),
        "bill_generated_at": record.get("bill_generated_at", ""),
        "stage": stage_of(record),
    }


def _llm(operation, what: str):
    """Run an LLM-backed step and turn its failure modes into clear API errors."""
    try:
        return operation()
    except json.JSONDecodeError as error:
        raise ApiError(502, "ai_invalid_response", f"{what}: the AI service returned an unreadable answer. Try again.") from error
    except ValueError as error:
        text = str(error)
        if ".env" in text:
            raise ApiError(503, "ai_not_configured", f"{what}: the AI service is not configured on the server ({text})") from error
        raise ApiError(502, "ai_invalid_response", f"{what}: {text}") from error
    except ConnectionError as error:
        raise ApiError(503, "ai_unreachable", f"{what}: the AI service could not be reached. Check the network and retry.") from error
    except FileNotFoundError as error:
        raise ApiError(503, "knowledge_base_missing", f"{what}: {error}") from error
    except RuntimeError as error:
        raise ApiError(502, "ai_failed", f"{what}: {error}") from error


def _agent():
    from app.agent.encounter_agent import EncounterAgent

    return EncounterAgent()


# Scribe drafts live on the server until a consultation is created from them (the client only holds an id),
# which makes "create" idempotent: a double click or a retry returns the same consultation.
_DRAFT_TTL_SECONDS = 4 * 3600
_drafts: dict[str, dict[str, Any]] = {}
_drafts_lock = threading.Lock()


def _store_draft(owner: str, context: dict[str, Any]) -> str:
    draft_id = secrets.token_urlsafe(16)
    now = time.monotonic()
    with _drafts_lock:
        for key in [k for k, v in _drafts.items() if now - v["created"] > _DRAFT_TTL_SECONDS]:
            _drafts.pop(key, None)
        _drafts[draft_id] = {"owner": owner, "created": now, "context": context, "record_id": None}
    return draft_id


def _scribe_result(draft_id: str, context: dict[str, Any]) -> dict[str, Any]:
    fields = ("chief_complaint", "hpi", "past_medical_history", "current_medications", "objective", "assessment", "plan")
    return {
        "draft_id": draft_id,
        "transcript": context.get("transcript", ""),
        "entries": parse_transcript_entries(context.get("transcript", "")),
        "soap_note": context.get("soap_note", ""),
        "key_highlights": context.get("key_highlights", []),
        "sections": {field: context.get(field, "") for field in fields},
        "word_count": len(str(context.get("transcript", "")).split()),
    }


def _validate_transcript(text: str) -> str:
    text = (text or "").strip()
    if len(text) < MIN_TRANSCRIPT_CHARS:
        raise ApiError(422, "transcript_too_short", "The conversation is too short to document. Paste or record the full consultation.")
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise ApiError(413, "transcript_too_long", f"The conversation is longer than {MAX_TRANSCRIPT_CHARS:,} characters. Split it into separate visits.")
    return text


def _run_scribe(caller: Caller, transcript: str) -> dict[str, Any]:
    context = _llm(lambda: _agent().prepare(transcript), "Ambient scribe")
    data = context.model_dump()
    return _scribe_result(_store_draft(caller.username, data), data)


# ---------------------------------------------------------------- patients (EHR Patient_Master CRUD)
def _consultation_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records.list_records():
        patient_id = ehr.record_patient_id(record)
        if patient_id:
            counts[patient_id] = counts.get(patient_id, 0) + 1
    return counts


@router.get("/patients")
def search_patients(q: str = "", limit: int = 25, offset: int = 0, with_counts: bool = False,
                    caller: Caller = Depends(requires("Clinical Review"))):
    query = q.strip().lower()
    # A role that sees identity masked can look patients up by patient ID only: searching by name or DOB
    # would reveal what the mask hides.
    search_keys = ("patient_id",) if caller.masking.hides(IDENTITY) else ("display_name", "patient_id", "MRN", "DOB")
    matches = []
    for patient in ehr.patients():
        haystack = " ".join(str(patient.get(key, "")) for key in search_keys).lower()
        if not query or query in haystack:
            matches.append(patient)
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    counts = _consultation_counts() if with_counts else {}
    rows = []
    for patient in matches[offset:offset + limit]:
        row = {
            "patient_id": patient.get("patient_id"),
            "name": patient.get("display_name") or "Unknown patient",
            "mrn": patient.get("MRN", ""),
            "dob": str(patient.get("DOB") or "")[:10],
            "sex": patient.get("Sex_at_Birth", ""),
        }
        if with_counts:
            row["consultations"] = counts.get(str(patient.get("patient_id")), 0)
            row["city"] = patient.get("City") or ""
        rows.append(row)
    return {"patients": rows, "total": len(matches), "offset": offset, "limit": limit}


@router.get("/patients/{patient_id}/context")
def patient_context(patient_id: str, caller: Caller = Depends(requires("Clinical Review"))):
    chart = ehr.chart(patient_id)
    if not chart:
        raise ApiError(404, "patient_not_found", f"EHR patient {patient_id} was not found.")
    coverage = primary_coverage(chart)
    return {
        "patient": chart.get("patient", {}),
        "context": chart_context(chart),
        "coverage": {"payer": coverage.get("Payer_Name", ""), "plan": coverage.get("Plan_Type", ""), "status": coverage.get("Status", "")},
        "allergies": [row.get("Substance") for row in active_drug_allergies(chart)],
    }


def _patient_detail(patient_id: str, caller: Caller) -> dict[str, Any]:
    patient = ehr.get_patient(patient_id)
    linked = [record_summary(r) for r in records.list_records() if ehr.record_patient_id(r) == str(patient_id)]
    chart_rows = ehr.chart_row_count(patient_id)
    can_delete = not caller.masking.hides(IDENTITY) and not linked and (caller.can("EHR Demo Data", "Edit") or (caller.can("Clinical Review", "Edit") and chart_rows == 0))
    if linked:
        reason = f"{len(linked)} consultation(s) belong to this patient. Delete those drafts first; signed notes are kept."
    elif caller.masking.hides(IDENTITY):
        reason = "Your role sees this patient's identity masked, so it cannot delete the patient."
    elif not can_delete:
        reason = "This patient has EHR chart history. Only a role with EHR Demo Data · Edit can delete them."
    else:
        reason = ""
    return {
        "patient": ehr.patient_view(patient),
        "consultations": linked,
        "chart_rows": chart_rows,
        "permissions": {"edit": caller.can("Clinical Review", "Edit"), "delete": can_delete, "delete_reason": reason,
                        "masked": sorted(caller.masking.masked)},
    }


def _validate_dob(value: str | None) -> str | None:
    if value is None:
        return value
    born = datetime.strptime(value, "%Y-%m-%d").date()
    if born > datetime.now().date() or born.year < 1900:
        raise ValueError("Date of birth must be a real date in the past")
    return value


_EMAIL = r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$"


class PatientFields(BaseModel):
    mobile: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=120, pattern=_EMAIL)
    street_address: str = Field(default="", max_length=120)
    city: str = Field(default="", max_length=60)
    state: str = Field(default="", max_length=30)
    zip: str = Field(default="", max_length=12)
    preferred_language: str = Field(default="", max_length=40)


class NewPatient(PatientFields):
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    dob: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sex: Literal["Male", "Female", "Other"] = "Other"
    allow_duplicate: bool = False

    @field_validator("dob")
    @classmethod
    def _past_date(cls, value):
        return _validate_dob(value)


@router.post("/patients")
def register_patient(body: NewPatient, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    first, last = body.first_name.strip(), body.last_name.strip()
    if not first or not last:
        raise ApiError(422, "validation_error", "First and last name are required.")
    duplicates = ehr.find_duplicates(first, last, body.dob)
    if duplicates and not body.allow_duplicate:
        ids = ", ".join(f"{p.get('Patient_ID')} ({p.get('MRN')})" for p in duplicates[:3])
        raise ApiError(409, "possible_duplicate",
                       f"A patient with the same name and date of birth already exists: {ids}. Search for them, or register anyway.",
                       {"patient_ids": ",".join(str(p.get("Patient_ID")) for p in duplicates)})
    extra = body.model_dump(include=set(PatientFields.model_fields))
    patient = ehr.create_patient(first, last, body.dob, body.sex, **extra)
    with store_transaction() as store:
        ac.audit(store, caller.username, "patient.create", f"{patient.get('Patient_ID')} {first} {last}")
    return {"patient_id": patient.get("Patient_ID") or patient.get("patient_id"), "mrn": patient.get("MRN", "")}


@router.get("/patients/{patient_id}")
def get_patient(patient_id: str, caller: Caller = Depends(requires("Clinical Review"))):
    return _patient_detail(patient_id, caller)


class PatientUpdate(BaseModel):
    etag: str
    first_name: str | None = Field(default=None, min_length=1, max_length=60)
    last_name: str | None = Field(default=None, min_length=1, max_length=60)
    dob: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    sex: Literal["Male", "Female", "Other"] | None = None
    mobile: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=120, pattern=_EMAIL)
    street_address: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=60)
    state: str | None = Field(default=None, max_length=30)
    zip: str | None = Field(default=None, max_length=12)
    preferred_language: str | None = Field(default=None, max_length=40)

    @field_validator("dob")
    @classmethod
    def _past_date(cls, value):
        return _validate_dob(value)


@router.put("/patients/{patient_id}")
def update_patient(patient_id: str, body: PatientUpdate, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    changes = {k: v.strip() for k, v in body.model_dump(exclude={"etag"}).items() if v is not None}
    for phi in sorted({field_class(k) for k in changes} - {None}):
        caller.require_phi(phi, "these patient details")
    if any(not changes.get(k, "x") for k in ("first_name", "last_name")):
        raise ApiError(422, "validation_error", "First and last name cannot be blank.")
    patient = ehr.update_patient(patient_id, body.etag, changes)
    # Consultations keep a copy of name / DOB / MRN for their headers: keep them in step with the EHR.
    view = ehr.patient_view(patient)
    for record in records.list_records():
        if ehr.record_patient_id(record) != str(patient_id):
            continue
        if (record.get("name"), str(record.get("dob") or "")[:10]) == (view["name"], view["dob"]):
            continue

        def sync(rec, v=view):
            rec["name"], rec["dob"], rec["mrn"] = v["name"], v["dob"], v["mrn"] or rec.get("mrn", "")

        records.mutate(record["id"], caller.username, None, sync)
    with store_transaction() as store:
        ac.audit(store, caller.username, "patient.update", f"{patient_id}: {', '.join(sorted(changes)) or 'no change'}")
    return _patient_detail(patient_id, caller)


@router.delete("/patients/{patient_id}")
def delete_patient(patient_id: str, etag: str, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    detail = _patient_detail(patient_id, caller)
    if detail["consultations"]:
        raise ApiError(409, "patient_in_use", detail["permissions"]["delete_reason"])
    if not detail["permissions"]["delete"]:
        raise ApiError(403, "forbidden", detail["permissions"]["delete_reason"])
    ehr.delete_patient(patient_id, etag)
    with store_transaction() as store:
        ac.audit(store, caller.username, "patient.delete", f"{patient_id} {detail['patient']['name']}")
    return {"deleted": patient_id}

# ---------------------------------------------------------------- ambient scribe
class TranscriptIn(BaseModel):
    transcript: str


@router.post("/scribe/text")
def scribe_text(body: TranscriptIn, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    return _run_scribe(caller, _validate_transcript(body.transcript))


def _audio_kind(head: bytes) -> str | None:
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    return None


@router.post("/scribe/audio")
def scribe_audio(file: UploadFile = File(...), caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    data = file.file.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise ApiError(422, "audio_empty", "The audio file is empty.")
    if len(data) > MAX_AUDIO_BYTES:
        raise ApiError(413, "audio_too_large", "Audio is larger than 25 MB. Record a shorter segment or paste the conversation.")
    if _audio_kind(data[:12]) is None:
        raise ApiError(415, "audio_format", "Use a WAV, FLAC or AIFF recording (the in-app recorder produces WAV).")
    try:
        import speech_recognition as sr
    except ImportError as error:
        raise ApiError(503, "speech_unavailable", "Speech recognition is not installed on the server.") from error
    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(io.BytesIO(data)) as source:
            transcript = recognizer.recognize_google(recognizer.record(source))
    except sr.UnknownValueError as error:
        raise ApiError(422, "no_speech", "No speech could be recognised. Record closer to the microphone.") from error
    except sr.RequestError as error:
        raise ApiError(503, "speech_unreachable", f"The speech service could not be reached: {error}") from error
    except ValueError as error:
        raise ApiError(415, "audio_unreadable", f"The audio could not be read: {error}") from error
    return _run_scribe(caller, _validate_transcript(transcript))


# ---------------------------------------------------------------- consultations
class NewConsultation(BaseModel):
    draft_id: str
    patient_id: str = Field(min_length=1)
    encounter_type: str = "Office Visit"

    @field_validator("encounter_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in ENCOUNTER_TYPES:
            raise ValueError(f"Choose one of: {', '.join(ENCOUNTER_TYPES)}")
        return value


@router.post("/consultations")
def create_consultation(body: NewConsultation, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    with _drafts_lock:
        draft = _drafts.get(body.draft_id)
        if draft is None:
            raise ApiError(410, "draft_expired", "This scribe draft expired (or the server restarted). Generate the summary again.")
        if draft["owner"] != caller.username:
            raise ApiError(403, "forbidden", "This scribe draft belongs to another user.")
        if draft["record_id"]:
            return {"id": draft["record_id"], "warnings": [], "duplicate": True}
        if draft.get("in_progress"):
            raise ApiError(409, "in_progress", "This consultation is already being created. Wait a moment.")
        draft["in_progress"] = True
    try:
        return _create_from_draft(draft, body, caller)
    finally:
        with _drafts_lock:
            draft["in_progress"] = False


def _create_from_draft(draft: dict[str, Any], body: NewConsultation, caller: Caller) -> dict[str, Any]:
    warnings: list[str] = []
    chart = ehr.chart(body.patient_id)
    if not chart:
        raise ApiError(404, "patient_not_found", f"EHR patient {body.patient_id} was not found.")
    patient = chart.get("patient", {})
    context = dict(draft["context"])
    try:
        context = _agent().add_clinical_decision_support(EncounterContext(**context)).model_dump()
    except Exception as error:  # noqa: BLE001 - CDS is advisory: save the consultation and say why it is missing
        logger.warning("CDS failed while creating a consultation: %s", error)
        warnings.append(f"Decision support is unavailable right now ({error}). Use “Regenerate CDS” on the review screen.")
    name = str(patient.get("Legal_Name") or " ".join(p for p in (patient.get("First_Name"), patient.get("Last_Name")) if p)).strip()
    now = datetime.now()
    record = {
        "id": f"PT-{uuid.uuid4().int % 10**8:08d}",
        "name": name or "Unknown patient",
        "age": "",
        "gender": patient.get("Sex_at_Birth", "Other"),
        "doctor": caller.name,
        "date": now.strftime("%d/%m/%Y"),
        "time": now.strftime("%H:%M"),
        "status": "Pending",
        "transcript": context.get("transcript", ""),
        "transcript_data": parse_transcript_entries(context.get("transcript", ""), caller.name, name or "Patient"),
        "summary": context.get("soap_note", ""),
        "patient_summary": context.get("patient_summary", ""),
        "key_highlights": context.get("key_highlights", []),
        "retrieved_guidelines": context.get("retrieved_guidelines", ""),
        "recommendations": context.get("recommendations", ""),
        "ehr_patient_id": body.patient_id,
        "patient_id": body.patient_id,
        "ehr_encounter_type": body.encounter_type,
        "dob": patient.get("DOB", ""),
        "cds_requested": bool(context.get("recommendations")),
        "analysis_version": ANALYSIS_VERSION,
        "created_by": caller.username,
    }
    try:
        ehr.register_consultation(record, body.encounter_type)
    except ApiError:
        raise
    except Exception as error:  # noqa: BLE001 - keep the clinical work; the EHR link can be retried later
        logger.warning("EHR registration failed: %s", error)
        warnings.append(f"Saved, but the EHR encounter could not be created: {error}")
    with store_transaction() as store:
        ac.assign_record(store, record, caller.username, caller.username, "Consultation created")
        ac.audit(store, caller.username, "consultation.create", f"{record['id']} {record['name']}")
    records.insert_record(record, caller.username)
    with _drafts_lock:
        draft["record_id"] = record["id"]
    return {"id": record["id"], "warnings": warnings, "duplicate": False}


@router.get("/consultations")
def list_consultations(caller: Caller = Depends(requires("Clinical Review"))):
    return {"consultations": [record_summary(r) for r in records.list_records()]}


def review_view(record: dict[str, Any], caller: Caller) -> dict[str, Any]:
    chart = ehr.chart(ehr.record_patient_id(record))
    sections = split_cds_sections(record.get("recommendations") or "")
    items = recommendation_items(sections["Recommendation"])
    insights = get_record_insights(record)
    is_approved = record.get("status") == "Approved"
    plan_text = f"{record.get('recommendations') or ''}\n{record.get('summary') or ''}"
    coverage = primary_coverage(chart) if chart else {}

    def checks(rows):
        return [{"label": a, "status": b, "tone": c, "detail": d} for a, b, c, d in rows]

    # Codes, coder decisions and bill data never travel to the clinical screen (segregation of duties).
    core = {key: value for key, value in record.items() if key not in CODING_FIELDS}
    return {
        "record": core,
        "summary": record_summary(record),
        "cds": {
            "items": items,
            "reasoning": sections["Reasoning"],
            "safety_note": sections["Safety Note"],
            "passages": guideline_passages(record.get("retrieved_guidelines")),
            "decisions": record.get("cds_decisions") or {},
        },
        "chart": {
            "linked": bool(chart),
            "context": chart_context(chart) if chart else None,
            "coverage": coverage.get("Plan_Type") or "",
        },
        "medication_safety": checks(medication_safety_checks(chart, plan_text)),
        "care_gaps": checks(care_gaps(chart)),
        "insights": [{"name": item, "category": get_insight_category(item)[0]} for item in insights],
        "handoff": "Queued for coding" if is_approved else "Awaiting signature",
        "permissions": {
            "edit": caller.can("Clinical Review", "Edit") and not is_approved and not caller.masking.hides(NARRATIVE),
            "sign": caller.can("Clinical Review", "Approve") and not is_approved and not caller.masking.hides(NARRATIVE),
        },
        "master_insights": [item for item in MASTER_INSIGHTS if item != "None"],
    }


@router.get("/consultations/{record_id}")
def get_consultation(record_id: str, caller: Caller = Depends(requires("Clinical Review"))):
    return review_view(records.get_record(record_id), caller)


@router.delete("/consultations/{record_id}")
def delete_consultation(record_id: str, version: int, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    """Delete a draft consultation. Signed notes are part of the legal record and are never deleted."""

    def guard(rec):
        if rec.get("status") == "Approved" or rec.get("bill_generated_at"):
            raise ApiError(409, "note_signed", "Signed notes are part of the legal medical record and cannot be deleted.")
        owner = rec.get("created_by")
        if owner and owner != caller.username and not caller.can("Admin Dashboard", "Edit"):
            raise ApiError(403, "forbidden", "Only the clinician who created this draft (or an administrator) can delete it.")

    record = records.delete_record(record_id, caller.username, version, guard)
    warning = ehr.cancel_consultation_links(record)
    with store_transaction() as store:
        ac.audit(store, caller.username, "consultation.delete", f"{record_id} {record.get('name', '')}")
    return {"deleted": record_id, "warnings": [w for w in (warning,) if w]}


class VersionedBody(BaseModel):
    version: int


def _ensure_draft(record: dict[str, Any]) -> None:
    if record.get("status") == "Approved":
        raise ApiError(409, "note_signed", "This note is signed and locked. It can no longer be changed.")


def _respond(record_id: str, caller: Caller, warnings: list[str] | None = None) -> dict[str, Any]:
    view = review_view(records.get_record(record_id), caller)
    view["warnings"] = [w for w in (warnings or []) if w]
    return view


@router.post("/consultations/{record_id}/cds/regenerate")
def regenerate_cds(record_id: str, body: VersionedBody, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    record = records.get_record(record_id)
    _ensure_draft(record)
    if records.version_of(record) != body.version:
        raise ApiError(409, "stale_record", "This consultation changed since you opened it. Reload first.")
    if not str(record.get("transcript", "")).strip():
        raise ApiError(422, "no_transcript", "This consultation has no conversation to analyse.")
    payload = {key: record.get(key, "") for key in ("transcript", "soap_note", "patient_summary", "chief_complaint", "hpi",
                                                   "past_medical_history", "current_medications", "objective", "assessment", "plan")}
    payload["soap_note"] = record.get("summary", "")
    payload["key_highlights"] = record.get("key_highlights", [])
    context = _llm(lambda: _agent().add_clinical_decision_support(EncounterContext(**payload)), "Decision support")
    if not str(context.recommendations or "").strip():
        raise ApiError(502, "cds_empty", "Decision support returned no recommendation. The previous one was kept.")

    def change(rec):
        _ensure_draft(rec)
        rec["retrieved_guidelines"] = context.retrieved_guidelines
        rec["recommendations"] = context.recommendations
        rec["cds_requested"] = True
        rec["analysis_version"] = ANALYSIS_VERSION
        rec["cds_decisions"] = {}
        log_history(rec, "CDS regenerated")

    records.mutate(record_id, caller.username, body.version, change)
    return _respond(record_id, caller)


class Decision(BaseModel):
    version: int
    status: Literal["accepted", "modified", "overridden"]
    note: str = Field(default="", max_length=2000)


@router.post("/consultations/{record_id}/recommendations/{index}/decision")
def decide_recommendation(record_id: str, index: int, body: Decision, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    if body.status in ("modified", "overridden") and not body.note.strip():
        raise ApiError(422, "note_required", "Give the modified wording or the reason for the override.")

    def change(rec):
        _ensure_draft(rec)
        items = recommendation_items(split_cds_sections(rec.get("recommendations") or "")["Recommendation"])
        if not 0 <= index < len(items):
            raise ApiError(404, "not_found", "That recommendation no longer exists (CDS may have been regenerated). Reload.")
        rec.setdefault("cds_decisions", {})[str(index)] = {
            "status": body.status, "note": body.note.strip(), "at": datetime.now().strftime("%d %b %H:%M"),
            "title": items[index]["title"], "by": caller.username,
        }
        log_history(rec, f"Recommendation {body.status}: {items[index]['title']}")

    records.mutate(record_id, caller.username, body.version, change)
    return _respond(record_id, caller)


class PlanLine(BaseModel):
    version: int
    line: str = Field(min_length=1, max_length=2000)


@router.post("/consultations/{record_id}/plan")
def push_to_plan(record_id: str, body: PlanLine, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    caller.require_phi(NARRATIVE, "the clinical note")
    def change(rec):
        _ensure_draft(rec)
        current = rec.get("summary") or rec.get("patient_summary", "")
        if body.line.strip().lower() in current.lower():
            return False  # already in the plan: pushing twice must not duplicate the line
        rec["summary"] = append_to_plan(current, body.line.strip())
        log_history(rec, f"Pushed to plan: {body.line.strip()}")
        return True

    added = records.mutate(record_id, caller.username, body.version, change)
    warning = ehr.sync_clinical_note(records.get_record(record_id)) if added else None
    return _respond(record_id, caller, [warning, None if added else "That item is already in the plan."])


class NoteBody(BaseModel):
    version: int
    summary: str = Field(max_length=MAX_TRANSCRIPT_CHARS)


@router.put("/consultations/{record_id}/note")
def save_note(record_id: str, body: NoteBody, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    caller.require_phi(NARRATIVE, "the clinical note")
    if not body.summary.strip():
        raise ApiError(422, "note_empty", "The SOAP note cannot be empty.")

    def change(rec):
        _ensure_draft(rec)
        rec["summary"] = body.summary
        log_history(rec, "Draft saved")

    records.mutate(record_id, caller.username, body.version, change)
    return _respond(record_id, caller, [ehr.sync_clinical_note(records.get_record(record_id))])


class InsightBody(BaseModel):
    version: int
    insights: list[str] = Field(min_length=1, max_length=6)


@router.post("/consultations/{record_id}/insights")
def add_insights(record_id: str, body: InsightBody, caller: Caller = Depends(requires("Clinical Review", "Edit"))):
    caller.require_phi(NARRATIVE, "the clinical note")
    unknown = [item for item in body.insights if item not in MASTER_INSIGHTS or item == "None"]
    if unknown:
        raise ApiError(422, "unknown_insight", f"Not a selectable insight: {', '.join(unknown)}")

    def change(rec):
        _ensure_draft(rec)
        manual = rec.setdefault("manual_insights", [])
        new = [item for item in dict.fromkeys(body.insights) if item not in manual]
        manual.extend(new)
        if new:
            rec["summary"] = append_selected_insights_to_note(rec.get("summary") or "", new)
            log_history(rec, f"Insights added: {', '.join(new)}")

    records.mutate(record_id, caller.username, body.version, change)
    return _respond(record_id, caller, [ehr.sync_clinical_note(records.get_record(record_id))])


class SignBody(BaseModel):
    version: int
    summary: str | None = Field(default=None, max_length=MAX_TRANSCRIPT_CHARS)


@router.post("/consultations/{record_id}/sign")
def sign_note(record_id: str, body: SignBody, caller: Caller = Depends(requires("Clinical Review", "Approve"))):
    caller.require_phi(NARRATIVE, "the clinical note")
    def change(rec):
        _ensure_draft(rec)
        if body.summary is not None:
            rec["summary"] = body.summary
        missing = missing_soap_sections(rec.get("summary") or "")
        if missing:
            raise ApiError(422, "soap_incomplete", "Add the missing SOAP section(s) before signing: " + ", ".join(missing))
        rec["status"] = "Approved"
        rec["approval_date"] = datetime.now().strftime("%d/%m/%Y")
        rec["signed_by"] = caller.username
        rec["coding_status"] = "Queued for coding"
        log_history(rec, f"Note signed and locked by {caller.name}")

    records.mutate(record_id, caller.username, body.version, change)
    record = records.get_record(record_id)
    warnings = [ehr.sync_clinical_note(record, approved=True)]
    try:
        save_approved_soap_note(record.get("ehr_encounter_id") or record_id, ehr.record_patient_id(record),
                                record.get("summary") or "", APPROVED_SOAP_DIR, actor=caller.username)
    except Exception as error:  # noqa: BLE001 - the signature stands; the archive copy is reported
        warnings.append(f"Signed, but the approved-note archive copy failed: {error}")
    with store_transaction() as store:
        ac.audit(store, caller.username, "note.sign", f"{record_id} {record.get('name', '')}")
    return _respond(record_id, caller, warnings)


class AssignBody(BaseModel):
    version: int
    username: str


@router.post("/consultations/{record_id}/assign")
def reassign(record_id: str, body: AssignBody, caller: Caller = Depends(requires("Admin Dashboard"))):
    target = ac.get_user(caller.store, body.username)
    if target is None or not target.get("active", True):
        raise ApiError(422, "unknown_user", "Choose an active user.")

    def change(rec):
        with store_transaction() as store:
            ac.assign_record(store, rec, body.username, caller.username, "Reassigned")

    records.mutate(record_id, caller.username, body.version, change)
    return {"summary": record_summary(records.get_record(record_id))}


@router.get("/meta")
def meta(caller: Caller = Depends(requires("Clinical Review"))):
    return {"encounter_types": list(ENCOUNTER_TYPES), "master_insights": [i for i in MASTER_INSIGHTS if i != "None"]}
