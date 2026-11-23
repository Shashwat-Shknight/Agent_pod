"""Coding & HCC (coders) and Billing & RCM (RCM analysts). Clinicians are blocked by the permission layer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.domain import access_control as ac
from app.domain.billing import billing_guardrail_rows, rcm_exceptions
from app.domain.claims import build_ambient_bill, register_generated_bill
from app.domain.coding import (
    EM_MDM_LEVELS,
    build_code_rows,
    coding_validation,
    encounter_code_results,
    meat_signals,
)
from app.domain.coding_validation import summarize as summarize_checks
from app.domain.encounter_pipeline import (
    build_estimated_hospital_bill,
    get_backend_code_suggestions,
    get_record_insights,
    normalize_billing_code,
)
from app.domain.settings import BILLING_NOTICE, CODE_MATCHER_VERSION
from app.web import ehr, records
from app.web.clinical_routes import record_summary
from app.web.context import Caller, requires, store_transaction
from app.web.errors import ApiError

router = APIRouter(prefix="/api", tags=["rcm"])

CODE_STATUSES = ("Accepted", "Rejected", "Query sent")


# ---------------------------------------------------------------- coding
def _code_signature(record: dict[str, Any]) -> str:
    payload = json.dumps([get_record_insights(record), record.get("transcript", ""), CODE_MATCHER_VERSION])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _merge_cpt_history(record: dict[str, Any], cpt_results: list[dict[str, Any]]) -> None:
    history = [item for item in record.get("cpt_results_history") or [] if isinstance(item, dict)]
    known = {(normalize_billing_code(i.get("CPT/HCPCS Code")), str(i.get("Extracted Procedure", "")).casefold()) for i in history}
    for candidate in cpt_results:
        identity = (normalize_billing_code(candidate.get("CPT/HCPCS Code")), str(candidate.get("Extracted Procedure", "")).casefold())
        if identity not in known:
            history.append(dict(candidate))
            known.add(identity)
    record["cpt_results_history"] = history


def ensure_codes(record_id: str, actor: str, force: bool = False) -> dict[str, Any]:
    """Match codes once per (insights, conversation, matcher version); later reads reuse the saved result."""
    record = records.get_record(record_id)
    signature = _code_signature(record)
    if not force and record.get("code_signature") == signature and isinstance(record.get("code_results"), dict):
        return record
    results = get_backend_code_suggestions(get_record_insights(record), record.get("transcript", ""))

    def change(rec):
        rec["code_results"] = {"icd10": list(results.get("icd10") or []), "cpt": list(results.get("cpt") or [])}
        _merge_cpt_history(rec, rec["code_results"]["cpt"])
        rec["code_signature"] = signature

    records.mutate(record_id, actor, None, change)
    return records.get_record(record_id)


def _checks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": c["name"], "status": c["status"], "detail": c["detail"]} for c in rows]


def coding_view(record: dict[str, Any], caller: Caller) -> dict[str, Any]:
    rows = build_code_rows(record, encounter_code_results(record))
    chart = ehr.chart(ehr.record_patient_id(record))
    pre, post = coding_validation(record, rows, chart)
    statuses = [row["status"] for row in rows]
    hccs = sorted({row["hcc"] for row in rows if row["hcc"] and row["status"] != "Rejected"})
    em_code = next((row["code"] for row in rows if row["code"] in EM_MDM_LEVELS and row["status"] != "Rejected"), None)
    for row in rows:
        if row["type"] == "ICD-10-CM":
            signals = meat_signals(f"{row['excerpt']} {record.get('summary', '')}" if row["excerpt"] else "")
            row["meat"] = signals
            row["cdi"] = "unspecified" in str(row["description"]).lower()
        if row["code"] in EM_MDM_LEVELS:
            row["mdm"] = EM_MDM_LEVELS[row["code"]]
    return {
        "summary": record_summary(record),
        "note": {"signed": record.get("status") == "Approved", "text": record.get("summary", ""),
                 "signed_by": record.get("signed_by", ""), "approval_date": record.get("approval_date", "")},
        "rows": rows,
        "pre_checks": {"checks": _checks(pre), **summarize_checks(pre)},
        "post_checks": {"checks": _checks(post), **summarize_checks(post)},
        "totals": {
            "icd": sum(row["type"] == "ICD-10-CM" for row in rows),
            "cpt": sum(row["type"].startswith("CPT") for row in rows),
            "accepted": statuses.count("Accepted"),
            "rejected": statuses.count("Rejected"),
            "queries": statuses.count("Query sent"),
            "hccs": hccs,
            "em_code": em_code,
            "mdm": EM_MDM_LEVELS.get(em_code, "") if em_code else "",
        },
        "audit": list(reversed(record.get("coding_audit") or []))[:100],
        "can_edit": caller.can("Codes", "Edit"),
        "matcher_version": CODE_MATCHER_VERSION,
    }


@router.get("/coding/queue")
def coding_queue(caller: Caller = Depends(requires("Codes"))):
    rows = []
    for record in records.list_records():
        decisions = record.get("code_decisions") or {}
        rows.append({**record_summary(record), "decided": len(decisions)})
    rows.sort(key=lambda row: (row["status"] != "Approved", row["bill_generated_at"] != ""))
    return {"queue": rows}


@router.get("/coding/{record_id}")
def get_coding(record_id: str, caller: Caller = Depends(requires("Codes"))):
    return coding_view(ensure_codes(record_id, caller.username), caller)


class RecomputeBody(BaseModel):
    version: int


@router.post("/coding/{record_id}/recompute")
def recompute_codes(record_id: str, body: RecomputeBody, caller: Caller = Depends(requires("Codes", "Edit"))):
    if records.version_of(records.get_record(record_id)) != body.version:
        raise ApiError(409, "stale_record", "This encounter changed since you opened it. Reload first.")
    return coding_view(ensure_codes(record_id, caller.username, force=True), caller)


class CodeDecision(BaseModel):
    version: int
    keys: list[str] = Field(min_length=1, max_length=50)
    status: Literal["Accepted", "Rejected", "Query sent"]
    note: str = Field(default="", max_length=1000)


@router.post("/coding/{record_id}/decisions")
def decide_codes(record_id: str, body: CodeDecision, caller: Caller = Depends(requires("Codes", "Edit"))):
    def change(rec):
        if rec.get("bill_generated_at"):
            raise ApiError(409, "already_billed", "This encounter was already sent to RCM. Coding is locked.")
        valid = {row["key"] for row in build_code_rows(rec, encounter_code_results(rec))}
        unknown = [key for key in body.keys if key not in valid]
        if unknown:
            raise ApiError(422, "unknown_code", f"Not on this encounter: {', '.join(unknown)}. Reload the coding screen.")
        stamp = datetime.now().strftime("%d %b %Y %H:%M")
        decisions = rec.setdefault("code_decisions", {})
        log = rec.setdefault("coding_audit", [])
        for key in dict.fromkeys(body.keys):
            decisions[key] = {"status": body.status, "at": stamp, "note": body.note, "by": caller.username}
            log.append({"at": stamp, "code": key, "action": body.status, "note": body.note, "by": caller.username})

    records.mutate(record_id, caller.username, body.version, change)
    with store_transaction() as store:
        ac.audit(store, caller.username, "coding.decision", f"{record_id}: {body.status} {', '.join(body.keys)}")
    return coding_view(records.get_record(record_id), caller)


# ---------------------------------------------------------------- billing
def _encounter_type(record: dict[str, Any]) -> str:
    return "IPD" if str(record.get("ehr_encounter_type", "")).lower().startswith("inp") else "OPD"


def _money(value: Any) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def billing_state(record: dict[str, Any]) -> dict[str, Any]:
    guardrail = billing_guardrail_rows(record)
    billable = [row for row in guardrail if row["guardrail"] == "BILLABLE"]
    held = [row for row in guardrail if row["guardrail"] in {"BLOCKED", "UNVERIFIED"}]
    encounter_type = _encounter_type(record)
    bill_rows, bill_total, unpriced = (
        build_estimated_hospital_bill([row["candidate"] for row in billable], encounter_type) if billable else ([], 0.0, [])
    )
    coded = [row for row in guardrail if row["guardrail"] != "EXCLUDED"]
    prices = {line["CPT/HCPCS Code"]: line for line in (
        build_estimated_hospital_bill([row["candidate"] for row in coded], encounter_type)[0] if coded else [])}
    held_value = sum(_money(prices.get(row["code"], {}).get("Gross Charge")) for row in held
                     if prices.get(row["code"], {}).get("Pricing Status") == "Priced")
    rows = build_code_rows(record, encounter_code_results(record)) if guardrail else []
    post = coding_validation(record, rows, ehr.chart(ehr.record_patient_id(record)))[1] if rows else []
    priced_rows = [row for row in bill_rows if row.get("Pricing Status") == "Priced"]
    return {
        "encounter_type": encounter_type,
        "guardrail": guardrail,
        "held": held,
        "bill_rows": bill_rows,
        "priced_rows": priced_rows,
        "total": bill_total,
        "unpriced": unpriced,
        "held_value": round(held_value, 2),
        "post_checks": post,
        "exceptions": rcm_exceptions(post, unpriced),
    }


def billing_view(record: dict[str, Any], caller: Caller) -> dict[str, Any]:
    state = billing_state(record)
    guardrail = [{key: value for key, value in row.items() if key != "candidate"} for row in state["guardrail"]]
    signed = record.get("status") == "Approved"
    blockers = []
    if not signed:
        blockers.append("The clinical note is not signed yet.")
    if not state["priced_rows"]:
        blockers.append("No billable, priced service on this encounter.")
    if not caller.can("RCM Workspace", "Approve"):
        blockers.append("Your role cannot release bills (RCM Workspace · Approve).")
    return {
        "summary": record_summary(record),
        "encounter_type": state["encounter_type"],
        "guardrail": guardrail,
        "bill_rows": state["bill_rows"],
        "total": state["total"],
        "held_value": state["held_value"],
        "unpriced": state["unpriced"],
        "post_checks": {"checks": _checks(state["post_checks"]), **summarize_checks(state["post_checks"])} if state["post_checks"] else None,
        "exceptions": state["exceptions"],
        "notice": BILLING_NOTICE,
        "sent": {"at": record.get("bill_generated_at", ""), "by": record.get("bill_released_by", ""),
                 "exceptions": record.get("rcm_exceptions") or []} if record.get("bill_generated_at") else None,
        "blockers": blockers,
        "can_override": caller.can("RCM Workspace", "Edit") or caller.can("Codes", "Edit"),
        "can_send": not blockers,
    }


@router.get("/billing/queue")
def billing_queue(caller: Caller = Depends(requires("RCM Workspace"))):
    return {"queue": [record_summary(r) for r in records.list_records() if r.get("status") == "Approved"]}


@router.get("/billing/{record_id}")
def get_billing(record_id: str, caller: Caller = Depends(requires("RCM Workspace"))):
    return billing_view(ensure_codes(record_id, caller.username), caller)


class OverrideBody(BaseModel):
    version: int
    codes: list[str] = Field(max_length=50)


@router.post("/billing/{record_id}/overrides")
def set_overrides(record_id: str, body: OverrideBody, caller: Caller = Depends(requires("RCM Workspace", "Edit"))):
    def change(rec):
        if rec.get("bill_generated_at"):
            raise ApiError(409, "already_billed", "This bill was already sent to RCM.")
        overridable = {row["code"] for row in billing_guardrail_rows(rec) if row["overridable"]}
        unknown = [code for code in body.codes if code not in overridable]
        if unknown:
            raise ApiError(422, "not_overridable", f"These lines are not held and cannot be overridden: {', '.join(unknown)}")
        previous = rec.get("billing_overrides") or {}
        stamp = {"by": caller.username, "at": datetime.now().strftime("%d %b %Y %H:%M")}
        rec["billing_overrides"] = {code: previous.get(code) or stamp for code in dict.fromkeys(body.codes)}

    records.mutate(record_id, caller.username, body.version, change)
    with store_transaction() as store:
        ac.audit(store, caller.username, "billing.override", f"{record_id}: {', '.join(body.codes) or 'none'}")
    return billing_view(records.get_record(record_id), caller)


class SendBody(BaseModel):
    version: int
    attested: bool
    resend: bool = False


@router.post("/billing/{record_id}/send")
def send_to_rcm(record_id: str, body: SendBody, caller: Caller = Depends(requires("RCM Workspace", "Approve"))):
    if not body.attested:
        raise ApiError(422, "attestation_required", "Confirm the attestation before sending the bill to RCM.")
    sent: dict[str, Any] = {}

    def change(rec):
        if rec.get("bill_generated_at") and not body.resend:
            raise ApiError(409, "already_sent", f"Already sent to RCM on {rec['bill_generated_at']} by {rec.get('bill_released_by')}.")
        if rec.get("status") != "Approved":
            raise ApiError(409, "note_unsigned", "The clinical note must be signed before the bill can go to RCM.")
        state = billing_state(rec)
        if not state["priced_rows"]:
            raise ApiError(409, "nothing_to_bill", "There is no billable, priced service to send.")
        estimate = {
            "record_id": rec.get("id"), "encounter_type": state["encounter_type"],
            "selected_cpt_codes": [row["CPT/HCPCS Code"] for row in state["priced_rows"]],
            "rows": state["priced_rows"], "total": state["total"], "unpriced_codes": state["unpriced"],
            "rcm_exceptions": state["exceptions"],
        }
        bill = build_ambient_bill(rec, estimate, encounter_code_results(rec))
        bill["rcm_exceptions"] = state["exceptions"]
        register_generated_bill(bill, source=state["encounter_type"], actor=caller.username)
        rec["bill_estimate"] = estimate
        rec["bill_generated_at"] = datetime.now().strftime("%d %b %Y %H:%M")
        rec["bill_released_by"] = caller.username
        rec["rcm_exceptions"] = state["exceptions"]
        rec["rcm_status"] = "Sent with exceptions" if state["exceptions"] else "Sent"
        sent.update(exceptions=len(state["exceptions"]), total=state["total"])

    records.mutate(record_id, caller.username, body.version, change)
    with store_transaction() as store:
        action = "claim.send_with_exceptions" if sent.get("exceptions") else "claim.send"
        ac.audit(store, caller.username, action, f"{record_id}: ${sent.get('total', 0):,.2f}")
    return billing_view(records.get_record(record_id), caller)


@router.get("/billing/{record_id}/pdf")
def bill_pdf(record_id: str, caller: Caller = Depends(requires("RCM Workspace"))):
    from app.domain.pdf_documents import archive_pdf, bill_pdf_bytes

    record = records.get_record(record_id)
    state = billing_state(record)
    if not state["bill_rows"]:
        raise ApiError(409, "nothing_to_bill", "There is no billable service on this encounter yet.")
    export = {
        "encounter_id": record.get("ehr_encounter_id") or record.get("id"),
        "patient": record.get("name"), "encounter_type": state["encounter_type"], "lines": state["bill_rows"],
        "gross_total": state["total"], "held_lines": [{"code": r["code"], "reason": r["reason"]} for r in state["held"]],
        "notice": BILLING_NOTICE,
    }
    # PDFs bypass the JSON masking middleware: apply the viewer's policy before rendering.
    policy = caller.masking
    export, record = policy.apply(export), policy.apply(record)
    content = bill_pdf_bytes(export, record)
    name = f"bill_{export['encounter_id']}.pdf"
    archive_pdf("DRAFT_BILL", name, content, actor=caller.username, encounter_id=str(export["encounter_id"] or ""),
                record_id=str(record.get("id") or ""), patient_name=str(record.get("name") or ""),
                metadata={"gross_total": state["total"], "line_count": len(state["bill_rows"])})
    return Response(content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})
