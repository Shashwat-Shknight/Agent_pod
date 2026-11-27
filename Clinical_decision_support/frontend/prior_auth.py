"""Prior authorization that runs in the background of ordering.

When a clinician orders a study that payers commonly gate (advanced imaging, nuclear cardiology,
echocardiography, sleep studies), a request is built from the order, the coverage and the note, and
submitted electronically (X12 278 / Da Vinci PAS style). The clinician only sees a small status badge;
staff work exceptions on the Prior Authorization page. Billing holds any performed service whose
authorization is not approved, so the claim is not denied for CARC 197.

The payer response is simulated: a request whose diagnoses support the service is approved with an
authorization number; one without a supporting diagnosis is pended for clinical information.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any

from app.rules import packs
from frontend import diagnostics as dx
from frontend.rcm import payer_class

REQUIRED, SUBMITTED, APPROVED, PENDED, DENIED, NOT_REQUIRED = (
    "Required", "Submitted", "Approved", "Pended", "Denied", "Not required",
)
CHANNEL = "ePA · X12 278 (simulated payer response)"


def rules() -> dict[str, Any]:
    """The prior_auth rule pack (CPT ranges, exempt payer classes, validity)."""
    return packs.load_pack("prior_auth")


def prior_auth_rule(code: Any) -> str | None:
    """Why this service needs authorization: the hospital CDM flag first, then the pack's CPT ranges."""
    from frontend.chargemaster import cdm_prior_auth_codes

    digits = re.sub(r"\D", "", str(code or ""))
    if str(code or "").strip().upper() in cdm_prior_auth_codes():
        return "Hospital CDM prior-authorization flag"
    if len(digits) != 5:
        return None
    value = int(digits)
    return next((rule["category"] for rule in rules()["cpt_ranges"] if rule["low"] <= value <= rule["high"]), None)


def requirement(cpt: str, coverage: dict[str, Any] | None) -> dict[str, Any]:
    """Does this payer need authorization for this service?"""
    rule = prior_auth_rule(cpt)
    klass = payer_class(coverage)
    if not rule:
        return {"required": False, "reason": "No authorization rule for this service"}
    if klass in set(rules().get("exempt_payer_classes") or ()):
        reason = "Self-pay: no payer authorization" if klass == "Self-pay" else f"{rule}: {klass} does not require prior authorization"
        return {"required": False, "reason": reason}
    return {"required": True, "reason": f"{rule}: {klass} plans require prior authorization", "category": rule}


def _number_from(seed: str, digits: int) -> str:
    return str(int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 10**digits).zfill(digits)


def build_request(order: dict[str, Any], record: dict[str, Any], coverage: dict[str, Any] | None,
                  submitted_by: str, now: datetime | None = None) -> dict[str, Any]:
    """Prior_Authorizations row assembled from the order, coverage and the clinical note."""
    now = now or datetime.now()
    test = dx.by_key()[order["test_key"]]
    coverage = coverage or {}
    assessment = dx._section(str(record.get("summary") or ""), "assessment")
    justification = " ".join(assessment.split())[:600] or str(record.get("patient_summary") or "")[:600]
    reasons = "; ".join(order.get("reasons") or [])
    return {
        "PA_ID": "",  # assigned by the EHR (PA########)
        "Patient_ID": str(record.get("ehr_patient_id") or ""),
        "MRN": str(record.get("mrn") or ""),
        "Encounter_ID": order.get("encounter_id") or "",
        "Order_ID": order.get("order_id") or "",
        "CPT_HCPCS": "+".join(test.cpts),
        "Service": test.name,
        "Payer_Name": str(coverage.get("Payer_Name") or ""),
        "Member_ID": str(coverage.get("Member_ID") or ""),
        "Plan_Type": str(coverage.get("Plan_Type") or ""),
        "Diagnosis_Codes": ";".join(order.get("linked_dx") or []),
        "Clinical_Justification": (justification + (f" | Order rationale: {reasons}" if reasons else "")).strip(),
        "Ordering_Provider": str(record.get("doctor") or ""),
        "Status": REQUIRED,
        "Channel": CHANNEL,
        "Submitted_At": "",
        "Submitted_By": submitted_by,
        "Decision_At": "",
        "Auth_Number": "",
        "Valid_From": "",
        "Valid_Through": "",
        "Approved_Units": "",
        "Determination_Reason": "",
    }


def submit(request: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Submit and record the (simulated) real-time payer determination."""
    now = now or datetime.now()
    row = dict(request)
    row["Submitted_At"] = now.strftime("%Y-%m-%d %H:%M")
    row["Status"] = SUBMITTED
    test = next((item for item in dx.catalog() if "+".join(item.cpts) == row.get("CPT_HCPCS")), None)
    diagnoses = [code for code in str(row.get("Diagnosis_Codes") or "").split(";") if code]
    supported = bool(test) and any(code.startswith(test.indications) for code in diagnoses)
    row["Decision_At"] = now.strftime("%Y-%m-%d %H:%M")
    if supported:
        start = now.date()
        row.update({
            "Status": APPROVED,
            "Auth_Number": "AUTH" + _number_from(f"{row.get('PA_ID')}|{row.get('Order_ID')}|{row.get('Member_ID')}", 9),
            "Valid_From": start.isoformat(),
            "Valid_Through": (start + timedelta(days=rules()["authorization_valid_days"])).isoformat(),
            "Approved_Units": rules().get("approved_units", 1),
            "Determination_Reason": "Meets criteria: diagnosis supports the requested service.",
        })
    else:
        row.update({
            "Status": PENDED,
            "Determination_Reason": "Pended: additional clinical information requested (no supporting diagnosis on the request).",
        })
    return row


def record_decision(request: dict[str, Any], status: str, *, auth_number: str = "", reason: str = "",
                    now: datetime | None = None) -> dict[str, Any]:
    """Staff entry of a payer decision received by phone/portal."""
    if status not in {APPROVED, DENIED, PENDED}:
        raise ValueError("Decision must be Approved, Denied or Pended.")
    if status == APPROVED and not auth_number.strip():
        raise ValueError("An approval needs the payer's authorization number.")
    now = now or datetime.now()
    row = dict(request)
    row.update({"Status": status, "Decision_At": now.strftime("%Y-%m-%d %H:%M"), "Determination_Reason": reason.strip()})
    if status == APPROVED:
        row.update({
            "Auth_Number": auth_number.strip(), "Valid_From": now.date().isoformat(),
            "Valid_Through": (now.date() + timedelta(days=rules()["authorization_valid_days"])).isoformat(), "Approved_Units": rules().get("approved_units", 1),
        })
    return row


def is_valid(request: dict[str, Any] | None, service_date: date) -> bool:
    if not request or request.get("Status") != APPROVED or not request.get("Auth_Number"):
        return False
    start = request.get("Valid_From")
    end = request.get("Valid_Through")
    try:
        return (not start or date.fromisoformat(str(start)[:10]) <= service_date) and (
            not end or service_date <= date.fromisoformat(str(end)[:10]))
    except ValueError:
        return False


def order_summary(request: dict[str, Any]) -> dict[str, Any]:
    """What the order keeps about its authorization."""
    return {
        "required": True,
        "pa_id": request.get("PA_ID") or "",
        "status": request.get("Status") or REQUIRED,
        "auth_number": request.get("Auth_Number") or "",
        "valid_through": request.get("Valid_Through") or "",
        "reason": request.get("Determination_Reason") or "",
    }
