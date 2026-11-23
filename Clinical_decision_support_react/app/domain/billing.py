"""Billing & RCM: performed-service guardrail, draft bill and hand-off to claim generation."""

import re
from datetime import datetime
from html import escape

import pandas as pd


from app.domain.claims import build_ambient_bill, register_generated_bill
from app.domain.clinical_text import best_excerpt
from app.domain.coding_validation import summarize as summarize_checks
from app.domain.ehr_chart import active_drug_allergies, age_from_dob, chart_rows, format_date, latest_first, primary_coverage
from app.domain.pdf_documents import bill_pdf_bytes
from app.domain.encounter_pipeline import (
    _procedure_status_inputs,
    build_estimated_hospital_bill,
    classify_procedure_status,
    is_explicitly_performed,
)
from app.domain.settings import BILLING_NOTICE
from app.domain.coding import build_code_rows, coding_validation, encounter_code_results, record_documentation







_EM_VISIT_CODES = {f"992{n:02d}" for n in range(2, 16)}  # 99202-99215 office/outpatient visits


def _is_em_visit(code, term):
    """The documented encounter itself is the evidence that the E/M visit happened."""
    return code in _EM_VISIT_CODES or "office visit" in str(term).lower()


def rcm_exceptions(post_checks, unpriced):
    """Open issues that travel with the claim to the RCM work queue instead of blocking the hand-off."""
    exceptions = [
        {"check": check["name"], "detail": check["detail"]} for check in post_checks or [] if check.get("status") == "fail"
    ]
    exceptions += [{"check": "Unpriced service", "detail": f"CPT {code} has no chargemaster rate; not on the claim"}
                   for code in unpriced or []]
    return exceptions


def billing_guardrail_rows(record):
    """Classify each CPT candidate as billable/blocked using performed-status evidence."""
    cpt_results = list(encounter_code_results(record).get("cpt") or [])
    documentation = record_documentation(record)
    decisions = record.get("code_decisions") or {}
    note_text = str(record.get("summary") or "").lower()
    rows = []
    for candidate in cpt_results:
        if not isinstance(candidate, dict):
            continue
        code = str(candidate.get("CPT/HCPCS Code") or "").strip()
        term = str(candidate.get("Extracted Procedure") or "")
        if not code:
            continue
        status_procedure, status_documentation = _procedure_status_inputs(term, documentation)
        procedure_status = (
            classify_procedure_status(status_procedure, status_documentation) if classify_procedure_status else "unknown"
        )
        performed = bool(is_explicitly_performed and is_explicitly_performed(status_procedure, status_documentation))
        decision = decisions.get(f"CPT:{code}", {}).get("status")
        rejected = decision == "Rejected"
        from_superbill = candidate.get("source") == "ocr_superbill"
        words = [word for word in re.findall(r"[a-z]+", term.lower()) if len(word) > 3]
        in_note = any(word in note_text for word in words)
        is_visit = _is_em_visit(code, term) and bool(documentation.strip())
        if rejected:
            guardrail, tone, reason = "EXCLUDED", "gray", "Rejected by coder"
        elif performed:
            guardrail, tone, reason = "BILLABLE", "green", "Performed and documented"
        elif is_visit:
            guardrail, tone, reason = "BILLABLE", "green", "Encounter documented (E/M visit)"
            procedure_status = "performed"
        elif from_superbill and decision == "Accepted":
            guardrail, tone, reason = "BILLABLE", "green", "Provider superbill (OCR), accepted by coder"
            procedure_status = "superbill"
        elif from_superbill:
            guardrail, tone, reason = "UNVERIFIED", "amber", "Superbill line awaiting coder acceptance"
            procedure_status = "superbill"
        elif procedure_status == "unknown":
            guardrail, tone, reason = "UNVERIFIED", "amber", "Performed status could not be verified"
        else:
            guardrail, tone, reason = "BLOCKED", "red", "Not documented as performed"
        held = guardrail in {"BLOCKED", "UNVERIFIED"}
        override = (record.get("billing_overrides") or {}).get(code) if held else None
        if override:
            guardrail, tone = "BILLABLE", "green"
            reason = f"Confirmed performed by {override.get('by') or 'coder'}"
        rows.append(
            {
                "candidate": candidate,
                "code": code,
                "service": candidate.get("Matched Procedure/Service") or term,
                "order": {
                    "performed": "Performed",
                    "ordered_not_performed": "Ordered",
                    "planned": "Planned",
                    "recommended": "Recommended",
                    "discussed": "Discussed",
                    "historical": "Historical",
                    "negated": "Not performed",
                    "superbill": "Superbill",
                }.get(procedure_status, "Unknown"),
                "evidence": best_excerpt(term, documentation, max_chars=90) or "No performed evidence found",
                "documentation": ("Signed note" if record.get("status") == "Approved" else "Draft note")
                if in_note
                else "Transcript only",
                "guardrail": guardrail,
                "tone": tone,
                "reason": reason,
                # Held by the guardrail but released by a coder; shown as an override choice.
                "overridden": bool(override),
                "overridable": held,
            }
        )
    return rows



def record_patient_id(record):
    return str((record or {}).get("ehr_patient_id") or (record or {}).get("patient_id") or "")


def _patient_key(record):
    return record_patient_id(record) or f"local:{str(record.get('name', '')).strip().lower()}"


def _billing_status(record):
    rows = billing_guardrail_rows(record)
    if record.get("bill_generated_at"):
        return "Bill drafted", "green"
    if not rows:
        return "Not coded", "gray"
    blocked = sum(row["guardrail"] in {"BLOCKED", "UNVERIFIED"} for row in rows)
    if blocked:
        return f"{blocked} line{'s' if blocked > 1 else ''} held", "red"
    if record.get("status") != "Approved":
        return "Awaiting signature", "amber"
    return "Ready to bill", "amber"





