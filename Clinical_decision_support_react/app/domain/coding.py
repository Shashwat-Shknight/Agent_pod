"""Medical Coding Workspace: code suggestions, evidence, coder decisions and coding validation."""

import re
from datetime import datetime
from html import escape

import pandas as pd


from app.domain import ocr_service as ocr
from app.domain.clinical_text import best_excerpt
from app.domain.coding_validation import post_coding_checks, pre_coding_checks
from app.domain.coding_validation import summarize as summarize_checks
from app.domain.ehr_chart import format_date
from app.domain.encounter_pipeline import (
    _procedure_status_inputs,
    classify_procedure_status,
    dedupe_code_results,
    get_backend_code_suggestions,
    get_code_results_signature,
    get_record_insights,
    normalize_billing_code,
)
from app.domain.settings import CODE_MATCHER_VERSION








# Small, explicitly labelled demo subset of CMS-HCC V28 mappings.
HCC_V28_DEMO = {
    "E10.9": "HCC 38",
    "E11.9": "HCC 38",
    "E11.42": "HCC 37",
    "E11.65": "HCC 38",
    "E66.01": "HCC 48",
    "I48.91": "HCC 238",
    "I50.9": "HCC 226",
    "J44.9": "HCC 280",
    "N18.4": "HCC 327",
    "N18.5": "HCC 326",
}

EM_MDM_LEVELS = {
    "99202": "Straightforward",
    "99203": "Low",
    "99204": "Moderate",
    "99205": "High",
    "99212": "Straightforward",
    "99213": "Low",
    "99214": "Moderate",
    "99215": "High",
}

CODE_STATUS_TONES = {
    "Accepted": "green",
    "Rejected": "red",
    "Query sent": "amber",
    "Suggested": "amber",
    "Symptom only": "violet",
    "Ordered": "violet",
    "Planned": "violet",
    "Discussed": "violet",
    "Performed": "blue",
    "E/M check": "amber",
    "Unverified": "gray",
    "Superbill": "blue",
}

MEAT_TERMS = {
    "M": ("monitor", "follow", "stable", "improv", "worse", "symptom", "progress", "history", "since"),
    "E": ("test", "result", "lab", "exam", "a1c", "x-ray", "level", "review", "vital", "reading"),
    "A": ("assess", "diagnos", "consistent", "likely", "impression", "controlled", "uncontrolled", "exacerbation"),
    "T": ("medication", "prescri", "continue", "start", "mg", "refer", "therapy", "treat", "inhaler", "metformin"),
}


def match_score(term, description):
    if not term or not description:
        return None
    try:
        from rapidfuzz import fuzz

        return int(round(fuzz.token_set_ratio(str(term).lower(), str(description).lower())))
    except ImportError:
        from difflib import SequenceMatcher

        return int(round(SequenceMatcher(None, str(term).lower(), str(description).lower()).ratio() * 100))


def record_documentation(record):
    """Transcript, SOAP note and any OCR'd supporting documents attached to the encounter."""
    documents = "\n".join(str(doc.get("text") or "") for doc in record.get("documents") or [])
    return f"{record.get('transcript', '')}\n{record.get('summary', '')}\n{documents}".strip()


def encounter_code_results(record, base=None):
    """Matcher suggestions plus codes imported from a paper superbill (OCR)."""
    base = base if base is not None else (record.get("code_results") or {})
    # One entry per code everywhere (coding table, billing guardrail, bill lines), even for older records.
    return dedupe_code_results(ocr.merge_code_results(base, record.get("imported_codes")))


def build_code_rows(record, code_results):
    documentation = record_documentation(record)
    decisions = record.get("code_decisions") or {}
    rows = []
    seen_icd = set()
    for item in code_results.get("icd10", []) or []:
        code = str(item.get("ICD-10 Code") or "").strip()
        if not code or code in seen_icd:
            continue
        seen_icd.add(code)
        term = item.get("Extracted Condition", "")
        auto_status = "Symptom only" if code.upper().startswith("R") else "Suggested"
        key = f"ICD:{code}"
        rows.append(
            {
                "key": key,
                "type": "ICD-10-CM",
                "code": code,
                "term": term,
                "description": item.get("Matched Disease/Injury", ""),
                "hcc": HCC_V28_DEMO.get(code.upper(), ""),
                "match": match_score(term, item.get("Matched Disease/Injury", "")),
                "status": decisions.get(key, {}).get("status") or auto_status,
                "excerpt": best_excerpt(term, documentation),
            }
        )
    seen_cpt = set()
    for item in code_results.get("cpt", []) or []:
        code = str(item.get("CPT/HCPCS Code") or "").strip()
        # One row per code: the same service can be found twice ("CT scan of chest" and "ct chest").
        if not code or code in seen_cpt:
            continue
        seen_cpt.add(code)
        term = item.get("Extracted Procedure", "")
        status_procedure, status_documentation = _procedure_status_inputs(term, documentation)
        procedure_status = (
            classify_procedure_status(status_procedure, status_documentation) if classify_procedure_status else "unknown"
        )
        from_superbill = item.get("source") == "ocr_superbill"
        if from_superbill and procedure_status != "performed":
            procedure_status = "superbill"
        elif procedure_status != "performed" and code in (record.get("billing_overrides") or {}):
            procedure_status = "coder_confirmed"
        if code in EM_MDM_LEVELS:
            auto_status = "E/M check"
        elif procedure_status == "superbill":
            auto_status = "Superbill"
        elif procedure_status == "coder_confirmed":
            auto_status = "Coder confirmed"
        else:
            auto_status = {
                "performed": "Performed",
                "ordered_not_performed": "Ordered",
                "planned": "Planned",
                "recommended": "Planned",
                "discussed": "Discussed",
            }.get(procedure_status, "Unverified")
        key = f"CPT:{code}"
        rows.append(
            {
                "key": key,
                "type": "CPT · E/M" if code in EM_MDM_LEVELS else "CPT",
                "code": code,
                "term": term,
                "description": item.get("Matched Procedure/Service", ""),
                "hcc": "",
                "match": match_score(term, item.get("Matched Procedure/Service", "")),
                "status": decisions.get(key, {}).get("status") or auto_status,
                "procedure_status": procedure_status,
                "excerpt": best_excerpt(term, documentation),
            }
        )
    return rows



def meat_signals(text):
    lowered = str(text or "").lower()
    return {letter: any(term in lowered for term in terms) for letter, terms in MEAT_TERMS.items()}





VALIDATION_TONES = {"pass": ("Pass", "green"), "warn": ("Warning", "amber"), "fail": ("Fail", "red")}


def run_claim_scrub(record, rows):
    """Run the project's claim scrubber over the non-rejected CPT codes; None if unavailable."""
    services = [
        {"CPT/HCPCS Code": row["code"], "Extracted Procedure": row["term"], "quantity": 1}
        for row in rows
        if str(row.get("type", "")).startswith("CPT") and row.get("status") != "Rejected"
    ]
    if not services:
        return {"errors": [], "warnings": []}
    encounter_type = "IPD" if str(record.get("ehr_encounter_type", "")).lower().startswith("inp") else "OPD"
    try:
        try:
            from app.domain.billing_scrubber import scrub_claim
        except ModuleNotFoundError:
            from billing_scrubber import scrub_claim
        report = scrub_claim(record_documentation(record), services, encounter_type=encounter_type)
    except Exception:
        return None
    return apply_coder_overrides(report, record.get("billing_overrides") or {})


def apply_coder_overrides(report, overrides):
    """A coder's recorded "confirmed performed" override answers the scrubber's documentation-support edit.

    Only ``unsupported_cpt`` is released (to a warning naming the coder); a wrong code for the documented
    service (``imaging_mismatch``) or an invalid / unpriced code still blocks billing.
    """
    if not report or not overrides:
        return report
    kept, released = [], []
    for issue in report.get("errors", []):
        override = overrides.get(str(issue.get("code") or ""))
        if issue.get("type") == "unsupported_cpt" and override:
            released.append({
                **issue,
                "type": "coder_attested",
                "message": f"CPT {issue['code']} is not in the note; released by {override.get('by') or 'coder'} "
                           f"({override.get('at') or 'override'}) as performed. Keep supporting documentation on file.",
            })
        else:
            kept.append(issue)
    return {**report, "errors": kept, "warnings": [*report.get("warnings", []), *released]}


def coding_validation(record, rows, chart=None):
    """Return (pre_checks, post_checks) for an encounter's current coding state."""
    pre = pre_coding_checks(record, chart or {}, get_record_insights(record))
    post = post_coding_checks(
        rows, record_documentation(record), meat_signals=meat_signals, scrub=run_claim_scrub(record, rows),
        code_set_results=official_code_set_results(record, rows),
    )
    return pre, post


def official_code_set_results(record, rows):
    """Validate the encounter's active codes against the official release in force on the visit date.

    Returns None (check skipped) offline or before the code sets have been synced.
    """
    from app.db.settings import use_snowflake

    if not use_snowflake():
        return None
    from app.db.client import StorageError
    from app.reference import lookup

    active = [row for row in rows if row.get("status") != "Rejected"]
    date_of_service = record.get("date") or datetime.now().strftime("%d/%m/%Y")
    try:
        if not lookup.code_sets_loaded():
            return None
        icd = [row["code"] for row in active if row.get("type") == "ICD-10-CM"]
        procedures = [row["code"] for row in active if str(row.get("type", "")).startswith("CPT")]
        return (lookup.validate_codes(icd, date_of_service, hint="ICD")
                + lookup.validate_codes(procedures, date_of_service, hint="CPT"))
    except StorageError:
        return None


