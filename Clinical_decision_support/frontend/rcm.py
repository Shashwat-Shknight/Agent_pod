"""Revenue cycle engine: eligibility & benefits, expected adjudication, patient accounts, aging and KPIs.

The billing screen used to stop at gross chargemaster charges. Real RCM continues:

1. Eligibility & benefits (X12 270/271): is coverage active on the date of service, and what is left
   of the deductible and out-of-pocket maximum this plan year?
2. Expected adjudication: allowed amount from the payer contract, contractual adjustment (CARC 45),
   then patient cost share in payer order - copay (PR-3) for office visits, deductible (PR-1), then
   coinsurance (PR-2) - capped at the out-of-pocket maximum. Lines without an approved authorization
   (CARC 197) or without a supporting diagnosis (CARC 50) are denied, not billed to the patient.
3. Remittance (X12 835): the payer pays its share; accumulators move; the patient gets a statement.
4. Patient A/R: due date, days past due, aging bucket, follow-up priority and the next best action.

Everything here is pure (no Streamlit, no I/O) so the same numbers appear on every screen and in tests.
Policy (contracts, plan designs, federal plan-year limits, statement and aging rules) comes from the
rcm_policy and claim_edits rule packs; payer contract rates are learned from the hospital chargemaster
when the payer appears there.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.rules import packs

EM_CODES = {f"992{n:02d}" for n in range(2, 16)}  # CPT E/M office/outpatient codes: a standard, kept in code


def policy() -> dict[str, Any]:
    """The RCM policy pack (contracts, plan designs, plan-year limits, statement and aging policy)."""
    return packs.load_pack("rcm_policy")


def carc_text() -> dict[str, str]:
    return policy()["carc_text"]


def statement_policy() -> dict[str, int]:
    return policy()["statement_policy"]


def year_limits(year: int) -> dict[str, Any]:
    """Federal plan-year limits (Part B deductible, ACA and MA out-of-pocket caps, Medicaid nominal copay)."""
    return packs.plan_year_values("rcm_policy", year)


def necessity_rules() -> dict[str, list[str]]:
    rules = packs.load_pack("claim_edits").get("medical_necessity") or {}
    return {code: prefixes for code, prefixes in rules.items() if not code.startswith("_")}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).replace("$", "").replace(",", "").strip()
        return float(text) if text not in ("", "None", "nan") else default
    except (TypeError, ValueError):
        return default


def money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d %b %Y %H:%M", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _iso(value: date | None) -> str:
    return value.isoformat() if value else ""


# ---------------------------------------------------------------- payer & benefits
def payer_class(coverage: dict[str, Any] | None) -> str:
    coverage = coverage or {}
    name = str(coverage.get("Payer_Name") or "").lower()
    plan = str(coverage.get("Plan_Type") or "").strip().lower()
    if not name and not plan:
        return "Self-pay"
    if "medicaid" in name or plan == "medicaid":
        return "Medicaid"
    if "medicare" in name:
        return "Medicare Advantage" if "advantage" in name else "Medicare"
    if plan in {"medicare", "medicare advantage"}:
        return "Medicare Advantage"
    return "Commercial"


def contract_terms(coverage: dict[str, Any] | None, encounter_type: str = "OPD") -> dict[str, Any]:
    """Allowed-to-charge ratio for this payer: learned from the hospital's charge lines when the payer
    appears there (overall and per CPT), else the policy pack's contract table, else the class default."""
    name = str((coverage or {}).get("Payer_Name") or "").strip().lower()
    klass = payer_class(coverage)
    if klass == "Self-pay":
        return {"allowed_rate": 1.0, "by_cpt": {}, "label": "Self-pay (gross charges)"}
    if name:
        from frontend.chargemaster import payer_contract_rates

        learned = payer_contract_rates(encounter_type)
        match = learned.get(name) or next((entry for key, entry in learned.items() if key in name or name in key), None)
        if match:
            return {"allowed_rate": match["allowed_rate"], "by_cpt": match["by_cpt"], "label": match["source"]}
        for contract in policy().get("payer_contracts_fallback") or []:
            if contract["match"] in name:
                return {"allowed_rate": contract["allowed_rate"], "by_cpt": {}, "label": contract["label"]}
    rate = policy()["payer_classes"][klass]["allowed_rate"]
    return {"allowed_rate": rate, "by_cpt": {}, "label": f"{klass} default fee schedule (policy pack)"}


def contract_for(coverage: dict[str, Any] | None) -> tuple[float, str]:
    terms = contract_terms(coverage)
    return terms["allowed_rate"], terms["label"]


def eligibility(coverage: dict[str, Any] | None, service_date: date) -> dict[str, str]:
    """Coverage status on the date of service (the 271 answer)."""
    coverage = coverage or {}
    if not coverage.get("Payer_Name"):
        return {"status": "No coverage", "detail": "No insurance on file; patient is self-pay."}
    status = str(coverage.get("Status") or "Active").strip().lower()
    effective, termination = parse_date(coverage.get("Effective_Date")), parse_date(coverage.get("Termination_Date"))
    if status not in {"active", ""}:
        return {"status": "Inactive", "detail": f"Coverage status is {coverage.get('Status')}."}
    if effective and service_date < effective:
        return {"status": "Inactive", "detail": f"Coverage starts {effective:%d %b %Y}, after the date of service."}
    if termination and service_date > termination:
        return {"status": "Inactive", "detail": f"Coverage ended {termination:%d %b %Y}, before the date of service."}
    return {"status": "Active", "detail": f"{coverage.get('Payer_Name')} · {coverage.get('Plan_Type') or 'plan'} active on {service_date:%d %b %Y}."}


def history_liability(chart: dict[str, Any] | None, patient_id: str, plan_year: int, exclude_encounter: str = "") -> float:
    """Patient liability already incurred this plan year, from the chart's paid claims (seeds accumulators)."""
    total = 0.0
    for row in ((chart or {}).get("records") or {}).get("Billing_Claims") or []:
        if not isinstance(row, dict) or str(row.get("Patient_ID")) != str(patient_id):
            continue
        if exclude_encounter and str(row.get("Encounter_ID")) == exclude_encounter:
            continue
        if str(row.get("Source") or "") == "NuuCare RCM":
            continue  # our own adjudications are tracked by the benefits accumulators
        served = parse_date(row.get("Service_Date"))
        if served is None or served.year != plan_year or str(row.get("Claim_Status") or "").lower() != "paid":
            continue
        liability = _number(row.get("Charge_USD")) - _number(row.get("Contractual_Adjustment_USD")) - _number(row.get("Payment_USD"))
        total += max(0.0, liability)
    return money(total)


def plan_benefits(
    coverage: dict[str, Any] | None,
    service_date: date,
    *,
    prior_liability: float = 0.0,
    accumulators: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Benefits snapshot for the date of service: copay, deductible, coinsurance, out-of-pocket max, accumulators.

    ``accumulators`` (deductible_met, oop_met) come from this patient's processed claims when we have
    adjudicated any; otherwise they are seeded from prior paid claims in the chart.
    """
    coverage = coverage or {}
    klass = payer_class(coverage)
    terms = contract_terms(coverage)
    rate, contract = terms["allowed_rate"], terms["label"]
    plan = str(coverage.get("Plan_Type") or "").strip().upper()
    defaults = policy()["payer_classes"][klass]
    limits = year_limits(service_date.year)
    coinsurance, oop_max = defaults["coinsurance"], defaults["oop_max"]
    if klass == "Commercial":
        design = (policy().get("commercial_plan_designs") or {}).get(plan) or {}
        coinsurance = design.get("coinsurance", coinsurance)
        oop_max = design.get("oop_max", oop_max)
        if limits.get("aca_oop_max_self") is not None and oop_max is not None:
            oop_max = min(oop_max, limits["aca_oop_max_self"])  # no plan may exceed the ACA limit
    elif klass == "Medicare Advantage" and limits.get("ma_moop_in_network") is not None and oop_max is not None:
        oop_max = min(oop_max, limits["ma_moop_in_network"])
    deductible = _number(coverage.get("Annual_Deductible_USD"))
    copay = _number(coverage.get("Copay_USD"))
    if klass == "Medicare":
        # Original Medicare Part B: the federal deductible for the year, then 20% coinsurance, no copay.
        deductible = float(limits.get("medicare_part_b_deductible", deductible))
        coinsurance = float(limits.get("medicare_part_b_coinsurance", coinsurance))
        copay = 0.0
    elif klass == "Medicaid":
        deductible, copay = 0.0, min(copay, float(limits.get("medicaid_nominal_outpatient_copay", 4.0)))
    elif klass == "Self-pay":
        deductible, copay = 0.0, 0.0
    if accumulators:
        deductible_met = min(deductible, _number(accumulators.get("deductible_met")))
        oop_met = _number(accumulators.get("oop_met"))
    else:
        deductible_met = min(deductible, prior_liability)
        oop_met = prior_liability
    if oop_max is not None:
        oop_met = min(oop_met, oop_max)
    status = eligibility(coverage, service_date)
    return {
        "payer": str(coverage.get("Payer_Name") or "Self-pay"),
        "plan_type": str(coverage.get("Plan_Type") or ""),
        "payer_class": klass,
        "member_id": str(coverage.get("Member_ID") or ""),
        "group_number": str(coverage.get("Group_Number") or ""),
        "relationship": str(coverage.get("Relationship_to_Subscriber") or "Self"),
        "eligibility_status": status["status"],
        "eligibility_detail": status["detail"],
        "eligibility_check_date": _iso(date.today()),
        "copay": money(copay),
        "annual_deductible": money(deductible),
        "deductible_met": money(deductible_met),
        "remaining_deductible": money(max(0.0, deductible - deductible_met)),
        "coinsurance": coinsurance,
        "oop_max": money(oop_max) if oop_max is not None else None,
        "oop_met": money(oop_met),
        "oop_remaining": money(max(0.0, oop_max - oop_met)) if oop_max is not None else None,
        "allowed_rate": rate,
        "allowed_rates_by_cpt": terms["by_cpt"],
        "contract": contract,
        "plan_year": service_date.year,
        "policy_version": policy()["version"],
        "plan_year_rules": limits.get("plan_year"),
    }


# ---------------------------------------------------------------- adjudication
@dataclass
class ClaimLine:
    code: str
    description: str
    charge: float
    dx: tuple[str, ...] = ()
    auth_required: bool = False
    auth_number: str = ""
    units: float = 1.0


def line_kind(code: str, dx: tuple[str, ...] | list[str], primary_dx: str = "") -> str:
    code = str(code).strip().upper()
    preventive_dx = tuple(policy().get("preventive_dx_prefixes") or ())
    if code in set(policy().get("preventive_codes") or ()):
        return "Preventive"
    if code in EM_CODES:
        if primary_dx and preventive_dx and primary_dx.upper().startswith(preventive_dx) and all(
            str(item).upper().startswith(preventive_dx) for item in dx or [primary_dx]
        ):
            return "Preventive"
        return "Office visit"
    return "Ancillary"


def necessity_supported(code: str, dx_codes: list[str] | tuple[str, ...]) -> bool:
    prefixes = tuple(necessity_rules().get(str(code).strip()) or ())
    if not prefixes:
        return True
    return any(str(item).upper().startswith(prefixes) for item in dx_codes)


def adjudicate(lines: list[ClaimLine], benefits: dict[str, Any], *, primary_dx: str = "") -> dict[str, Any]:
    """Expected payer adjudication, line by line, in claim order (what the 835 should say)."""
    rate = float(benefits.get("allowed_rate") or 1.0)
    by_cpt = benefits.get("allowed_rates_by_cpt") or {}
    coins_pct = float(benefits.get("coinsurance") or 0.0)
    remaining_deductible = float(benefits.get("remaining_deductible") or 0.0)
    oop_remaining = benefits.get("oop_remaining")
    oop_remaining = None if oop_remaining is None else float(oop_remaining)
    copay_amount = float(benefits.get("copay") or 0.0)
    klass = benefits.get("payer_class", "Commercial")
    inactive = benefits.get("eligibility_status") != "Active" and klass != "Self-pay"
    copay_taken = False
    out_lines, totals = [], dict.fromkeys(
        ("charge", "allowed", "contractual", "denied", "copay", "deductible", "coinsurance", "patient", "payer"), 0.0
    )
    for line in lines:
        charge = money(line.charge)
        kind = line_kind(line.code, line.dx, primary_dx)
        row = {
            "code": line.code, "description": line.description, "kind": kind, "charge": charge, "allowed": 0.0,
            "contractual": 0.0, "denied": 0.0, "copay": 0.0, "deductible": 0.0, "coinsurance": 0.0,
            "patient": 0.0, "payer": 0.0, "carc": [], "dx": list(line.dx), "auth_number": line.auth_number,
        }
        if inactive:
            row.update(denied=charge, carc=["CO-27"])
        elif line.auth_required and not line.auth_number:
            row.update(denied=charge, carc=["CO-197"])
        elif not necessity_supported(line.code, line.dx):
            row.update(denied=charge, carc=["CO-50"])
        else:
            allowed = money(charge * float(by_cpt.get(str(line.code).strip().upper(), rate)))
            row["allowed"] = allowed
            row["contractual"] = money(charge - allowed)
            if row["contractual"] > 0:
                row["carc"].append("CO-45")
            copay = deductible = coinsurance = 0.0
            if klass == "Self-pay":
                coinsurance = allowed
            elif kind == "Preventive":
                pass  # ACA preventive services: no patient cost share
            elif kind == "Office visit" and copay_amount and not copay_taken:
                copay = min(copay_amount, allowed)
                copay_taken = True
            elif kind == "Office visit" and copay_amount:
                pass  # one copay per visit
            else:
                deductible = min(remaining_deductible, allowed)
                coinsurance = money((allowed - deductible) * coins_pct)
            patient = copay + deductible + coinsurance
            if oop_remaining is not None and patient > oop_remaining:
                excess = patient - oop_remaining
                cut = min(coinsurance, excess)
                coinsurance -= cut
                excess -= cut
                cut = min(deductible, excess)
                deductible -= cut
                excess -= cut
                copay -= min(copay, excess)
                patient = copay + deductible + coinsurance
            remaining_deductible = max(0.0, remaining_deductible - deductible)
            if oop_remaining is not None:
                oop_remaining = max(0.0, oop_remaining - patient)
            row.update(copay=money(copay), deductible=money(deductible), coinsurance=money(coinsurance),
                       patient=money(patient), payer=money(allowed - patient))
            row["carc"] += [code for code, amount in (("PR-3", copay), ("PR-1", deductible), ("PR-2", coinsurance)) if amount > 0]
        for key in totals:
            totals[key] += row[key]
        out_lines.append(row)
    totals = {key: money(value) for key, value in totals.items()}
    return {
        "lines": out_lines,
        "totals": totals,
        "remaining_deductible_after": money(remaining_deductible),
        "oop_remaining_after": None if oop_remaining is None else money(oop_remaining),
        "denied_lines": [row for row in out_lines if row["denied"]],
        "status": claim_status_from(out_lines),
    }


def claim_status_from(lines: list[dict[str, Any]]) -> str:
    if not lines:
        return "No lines"
    denied = sum(1 for row in lines if row["denied"])
    if denied == len(lines):
        return "Denied"
    return "Partially denied" if denied else "Paid"


def control_number(seed: str) -> str:
    """Deterministic payer claim control number (CLP07) so a re-posted remittance stays the same."""
    return "PCN" + str(int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 10**11).zfill(11)


# ---------------------------------------------------------------- patient account & aging
def aging_bucket(balance: float, statement_date: date | None, due_date: date | None, today: date) -> tuple[str, int]:
    if balance <= 0.004:
        return ("Credit balance" if balance < -0.004 else "No balance"), 0
    if statement_date is None or due_date is None:
        return "Statement pending", 0
    days_past_due = max(0, (today - due_date).days)
    if days_past_due == 0:
        return "Current", 0
    for bucket in policy()["aging_buckets"]:
        if bucket["max_days"] is None or days_past_due <= bucket["max_days"]:
            return bucket["label"], days_past_due
    return policy()["aging_buckets"][-1]["label"], days_past_due


AGING_ORDER = (
    "Not yet billed", "With payer", "Statement pending", "Current", "1-30", "31-60", "61-90", "90+", "No balance", "Credit balance",
)


def follow_up_priority(balance: float, days_past_due: int, claim_status: str) -> str:
    if claim_status in {"Denied", "Partially denied"}:
        return "High"
    if balance <= 0.004:
        return "None" if balance > -0.004 else "Medium"
    thresholds = policy()["follow_up_priority"]
    if days_past_due > thresholds["high_days_past_due"] or balance >= thresholds["high_balance"]:
        return "High"
    if days_past_due > 0 or balance >= thresholds["medium_balance"]:
        return "Medium"
    return "Low"


def recommended_action(account: dict[str, Any], today: date) -> str:
    balance = _number(account.get("Delta_Patient_Balance"))
    status = str(account.get("Claim_Status") or "")
    bucket = str(account.get("Aging_Bucket") or "")
    days = int(_number(account.get("Days_Past_Due")))
    if status in {"Denied", "Partially denied"}:
        reasons = str(account.get("Denial_Reasons") or "")
        if "CO-197" in reasons:
            return "Work denial: request retro-authorization, then appeal (CARC 197)"
        if "CO-50" in reasons:
            return "Work denial: add supporting diagnosis / medical-necessity appeal (CARC 50)"
        if "CO-27" in reasons:
            return "Work denial: verify coverage dates; bill secondary or patient (CARC 27)"
        return "Work denial with payer"
    if status in {"Ready to submit", "Draft", ""}:
        return "Submit claim to payer"
    if status in {"Submitted", "Accepted", "Rejected"}:
        return "Fix clearinghouse rejection and resubmit" if status == "Rejected" else "Await payer adjudication; no patient statement yet"
    if balance < -0.004:
        return "Refund patient credit balance"
    if balance <= 0.004:
        return "Close — no patient balance"
    if bucket == "Statement pending":
        return "Send first patient statement"
    if bucket == "Current":
        return "Statement sent; monitor and offer online payment"
    if bucket == "1-30":
        return "Send reminder statement with text/email nudge"
    if bucket == "31-60":
        return "Call patient; offer payment plan" + ("" if balance < 1000 else " or financial-assistance screening")
    if bucket == "61-90":
        return "Screen for financial assistance; send final notice"
    if days < statement_policy()["extraordinary_collection_wait_days"]:
        return "Final notice; no collection action before day 120 (IRS 501(r))"
    return "Pre-collection review (501(r) notice period met)"


def refresh_account(account: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Recompute balance, due date, aging, priority and next action for a Patient_Balance row."""
    today = today or date.today()
    row = dict(account)
    responsibility = _number(row.get("Patient_Responsibility"))
    paid = _number(row.get("Patient_Paid_At_Discharge")) + _number(row.get("Patient_Payments"))
    assistance = _number(row.get("Financial_Assistance"))
    balance = money(responsibility - paid - assistance)
    row["Delta_Patient_Balance"] = balance
    statement = parse_date(row.get("Statement_Date"))
    due = parse_date(row.get("Payment_Due_Date"))
    if statement and not due:
        due = statement + timedelta(days=statement_policy()["terms_days"])
        row["Payment_Due_Date"] = _iso(due)
    status = str(row.get("Claim_Status") or "")
    if status in {"Ready to submit", "Draft", ""}:
        bucket, days = "Not yet billed", 0  # the claim has not left the building
    elif status not in {"Paid", "Partially denied", "Denied"}:
        bucket, days = "With payer", 0  # no patient statement before the payer adjudicates
    else:
        bucket, days = aging_bucket(balance, statement, due, today)
    row["Aging_Bucket"] = bucket
    row["Days_Past_Due"] = days
    row["Follow_up_Priority"] = follow_up_priority(balance, days, status)
    row["Recommended_Action"] = recommended_action(row, today)
    row["Last_Updated"] = today.isoformat()
    return row


def new_account(
    *,
    patient_id: str,
    mrn: str,
    encounter_id: str,
    patient_name: str,
    service_date: date,
    benefits: dict[str, Any],
    adjudication: dict[str, Any],
    guarantor: str,
    claim_ids: list[str] | None = None,
    collected_at_visit: float = 0.0,
) -> dict[str, Any]:
    """Patient_Balance row created when an encounter is sent to RCM (expected amounts, not yet billed)."""
    totals = adjudication["totals"]
    row = {
        "Account_ID": encounter_id,
        "Patient_ID": patient_id,
        "MRN": mrn,
        "Encounter_ID": encounter_id,
        "Patient_Name": patient_name,
        "Service_Date": _iso(service_date),
        "Payer": benefits.get("payer"),
        "Plan_Type": benefits.get("plan_type"),
        "Claim_IDs": ";".join(claim_ids or []),
        "Charge": totals["charge"],
        "Allowed_Amount": totals["allowed"],
        "Contractual_Adjustment": totals["contractual"],
        "Denied_Amount": totals["denied"],
        "Remaining_Deductible": benefits.get("remaining_deductible"),
        "OOP_Remaining": benefits.get("oop_remaining"),
        "Copay_Applied": totals["copay"],
        "Deductible_Applied": totals["deductible"],
        "Coinsurance_Applied": totals["coinsurance"],
        "Patient_Responsibility": totals["patient"],
        "Payer_Expected_Payment": totals["payer"],
        "Payer_Paid": 0.0,
        "Patient_Paid_At_Discharge": money(collected_at_visit),
        "Patient_Payments": 0.0,
        "Financial_Assistance": 0.0,
        "Claim_Status": "Ready to submit",
        "Denial_Reasons": "",
        "Insurance_Adjudication_Date": "",
        "Statement_Date": "",
        "Payment_Due_Date": "",
        "Guarantor": guarantor,
        "Guarantor_Relationship": benefits.get("relationship") or "Self",
        "Source": "NuuCare RCM",
    }
    return refresh_account(row)


def post_remittance(account: dict[str, Any], adjudication: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Apply the 835: payer paid its share, patient responsibility is final, first statement goes out."""
    today = today or date.today()
    totals = adjudication["totals"]
    row = dict(account)
    row.update({
        "Allowed_Amount": totals["allowed"],
        "Contractual_Adjustment": totals["contractual"],
        "Denied_Amount": totals["denied"],
        "Copay_Applied": totals["copay"],
        "Deductible_Applied": totals["deductible"],
        "Coinsurance_Applied": totals["coinsurance"],
        "Patient_Responsibility": totals["patient"],
        "Payer_Expected_Payment": totals["payer"],
        "Payer_Paid": totals["payer"],
        "Claim_Status": adjudication["status"],
        "Denial_Reasons": ";".join(sorted({code for line in adjudication["denied_lines"] for code in line["carc"]})),
        "Insurance_Adjudication_Date": today.isoformat(),
        "Remaining_Deductible": adjudication.get("remaining_deductible_after"),
        "OOP_Remaining": adjudication.get("oop_remaining_after"),
    })
    paid_already = _number(row.get("Patient_Paid_At_Discharge")) + _number(row.get("Patient_Payments"))
    if totals["patient"] - paid_already > 0.004:
        terms = statement_policy()
        statement = today + timedelta(days=terms["first_statement_delay_days"])
        row["Statement_Date"] = statement.isoformat()
        row["Payment_Due_Date"] = (statement + timedelta(days=terms["terms_days"])).isoformat()
    return refresh_account(row, today)


def apply_payment(account: dict[str, Any], amount: float, *, kind: str = "payment", today: date | None = None) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError("Enter an amount greater than zero.")
    row = dict(account)
    field = {"payment": "Patient_Payments", "assistance": "Financial_Assistance", "visit": "Patient_Paid_At_Discharge"}[kind]
    row[field] = money(_number(row.get(field)) + amount)
    return refresh_account(row, today)


def benefits_row(benefits: dict[str, Any], *, patient_id: str, mrn: str, encounter_id: str, guarantor: str,
                 account: dict[str, Any] | None = None) -> dict[str, Any]:
    """Payer_Plan_Benefits row (the 271 snapshot for this encounter, plus where the claim stands)."""
    account = account or {}
    return {
        "Benefit_Key": encounter_id,
        "Patient_ID": patient_id,
        "MRN": mrn,
        "Encounter_ID": encounter_id,
        "Payer": benefits["payer"],
        "Plan_Type": benefits["plan_type"],
        "Payer_Class": benefits["payer_class"],
        "Member_ID": benefits["member_id"],
        "Group_Number": benefits["group_number"],
        "Eligibility_Status": benefits["eligibility_status"],
        "Eligibility_Check_Date": benefits["eligibility_check_date"],
        "Copay": benefits["copay"],
        "Annual_Deductible": benefits["annual_deductible"],
        "Deductible_Met_YTD": benefits["deductible_met"],
        "Coinsurance_Pct": benefits["coinsurance"],
        "OOP_Max": benefits["oop_max"],
        "OOP_Met_YTD": benefits["oop_met"],
        "Allowed_Rate": benefits["allowed_rate"],
        "Contract": benefits["contract"],
        "Plan_Year": benefits["plan_year"],
        "Claim_Status": account.get("Claim_Status", "Ready to submit"),
        "Guarantor": guarantor,
        "Guarantor_Relationship": benefits["relationship"],
        "Insurance_Adjudication_Date": account.get("Insurance_Adjudication_Date", ""),
        "Statement_Date": account.get("Statement_Date", ""),
        "Payment_Due_Date": account.get("Payment_Due_Date", ""),
        "Deductible_Met_After": "",
        "OOP_Met_After": "",
    }


def accumulators_from(rows: list[dict[str, Any]], member_id: str, plan_year: int) -> dict[str, float] | None:
    """Latest processed accumulators for this member and plan year, from adjudicated encounters."""
    processed = [
        row for row in rows
        if str(row.get("Member_ID")) == str(member_id) and str(row.get("Plan_Year")) == str(plan_year)
        and str(row.get("Deductible_Met_After") or "") != ""
    ]
    if not processed:
        return None
    latest = max(processed, key=lambda row: (str(row.get("Insurance_Adjudication_Date") or ""), _number(row.get("OOP_Met_After"))))
    return {"deductible_met": _number(latest.get("Deductible_Met_After")), "oop_met": _number(latest.get("OOP_Met_After"))}


def guarantor_name(patient_name: str, coverage: dict[str, Any] | None) -> str:
    relationship = str((coverage or {}).get("Relationship_to_Subscriber") or "Self").strip()
    return patient_name if relationship.lower() in {"self", ""} else f"Subscriber ({relationship})"


# ---------------------------------------------------------------- legacy chart claims as accounts
def accounts_from_chart_claims(claims: list[dict[str, Any]], patients: dict[str, dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Patient accounts derived from the EHR's historical Billing_Claims (one per encounter).

    Paid claims with a patient balance were statemented two weeks after service; submitted or pending
    claims are still with the payer. Used so the A/R worklist reflects the chart's existing history.
    """
    by_encounter: dict[str, dict[str, Any]] = {}
    for claim in claims:
        if not isinstance(claim, dict) or str(claim.get("Source") or "") == "NuuCare RCM":
            continue
        encounter_id = str(claim.get("Encounter_ID") or "")
        if not encounter_id or str(claim.get("Claim_ID") or "").startswith("DRAFT-"):
            continue
        patient = patients.get(str(claim.get("Patient_ID")), {})
        charge = _number(claim.get("Charge_USD"))
        adjustment = _number(claim.get("Contractual_Adjustment_USD"))
        payment = _number(claim.get("Payment_USD"))
        status = str(claim.get("Claim_Status") or "")
        served = parse_date(claim.get("Service_Date"))
        account = by_encounter.setdefault(encounter_id, {
            "Account_ID": encounter_id, "Patient_ID": str(claim.get("Patient_ID") or ""),
            "MRN": str(patient.get("MRN") or ""), "Encounter_ID": encounter_id,
            "Patient_Name": str(patient.get("Legal_Name") or ""), "Service_Date": _iso(served),
            "Payer": str(claim.get("Payer") or ""), "Claim_IDs": "", "Charge": 0.0, "Allowed_Amount": 0.0,
            "Contractual_Adjustment": 0.0, "Denied_Amount": 0.0, "Patient_Responsibility": 0.0,
            "Payer_Expected_Payment": 0.0, "Payer_Paid": 0.0, "Patient_Paid_At_Discharge": 0.0,
            "Patient_Payments": 0.0, "Financial_Assistance": 0.0, "Copay_Applied": "", "Deductible_Applied": "",
            "Coinsurance_Applied": "", "Claim_Status": {"paid": "Paid", "submitted": "Submitted"}.get(status.lower(), status or "Pending"),
            "Insurance_Adjudication_Date": "", "Statement_Date": "", "Payment_Due_Date": "",
            "Source": "EHR claim history", "Denial_Reasons": "",
        })
        account["Claim_IDs"] = ";".join(filter(None, dict.fromkeys([*account["Claim_IDs"].split(";"), str(claim.get("Claim_ID") or "")])))
        account["Charge"] = money(account["Charge"] + charge)
        account["Contractual_Adjustment"] = money(account["Contractual_Adjustment"] + adjustment)
        account["Allowed_Amount"] = money(account["Charge"] - account["Contractual_Adjustment"])
        if status.lower() == "paid":
            account["Payer_Paid"] = money(account["Payer_Paid"] + payment)
            account["Payer_Expected_Payment"] = account["Payer_Paid"]
            account["Patient_Responsibility"] = money(account["Patient_Responsibility"] + max(0.0, charge - adjustment - payment))
            if served:
                adjudicated = served + timedelta(days=14)
                account["Insurance_Adjudication_Date"] = adjudicated.isoformat()
                if account["Patient_Responsibility"] > 0:
                    terms = statement_policy()
                    statement = adjudicated + timedelta(days=terms["first_statement_delay_days"])
                    account["Statement_Date"] = statement.isoformat()
                    account["Payment_Due_Date"] = (statement + timedelta(days=terms["terms_days"])).isoformat()
        else:
            account["Payer_Expected_Payment"] = money(account["Payer_Expected_Payment"] + payment)
    return [refresh_account(account, today) for account in by_encounter.values()]


# ---------------------------------------------------------------- KPIs
def kpis(accounts: list[dict[str, Any]], claims: list[dict[str, Any]] | None = None, today: date | None = None) -> dict[str, Any]:
    """PATIENT_ACCOUNT_SUMMARY KPIs over a set of patient accounts (and generated claims when given)."""
    today = today or date.today()
    total = lambda key: money(sum(_number(row.get(key)) for row in accounts))  # noqa: E731
    charges, contractual = total("Charge"), total("Contractual_Adjustment")
    patient_resp, at_visit = total("Patient_Responsibility"), total("Patient_Paid_At_Discharge")
    payer_paid, patient_paid = total("Payer_Paid"), total("Patient_Payments")
    assistance, denied = total("Financial_Assistance"), total("Denied_Amount")
    open_rows = [row for row in accounts if _number(row.get("Delta_Patient_Balance")) > 0.004]
    outstanding = money(sum(_number(row.get("Delta_Patient_Balance")) for row in open_rows))
    by_bucket = {bucket: 0.0 for bucket in AGING_ORDER}
    for row in accounts:
        bucket = row.get("Aging_Bucket") or "Not yet billed"
        by_bucket[bucket] = money(by_bucket.get(bucket, 0.0) + max(0.0, _number(row.get("Delta_Patient_Balance"))))
    over_90 = by_bucket.get("90+", 0.0)
    adjudicated = [row for row in accounts if row.get("Claim_Status") in {"Paid", "Partially denied", "Denied"}]
    denied_accounts = [row for row in adjudicated if row.get("Claim_Status") in {"Denied", "Partially denied"}]
    served = [parse_date(row.get("Service_Date")) for row in accounts]
    recent_charges = sum(_number(row.get("Charge")) for row, when in zip(accounts, served) if when and (today - when).days <= 90)
    total_ar = outstanding + money(sum(
        _number(row.get("Payer_Expected_Payment")) for row in accounts if row.get("Claim_Status") in {"Ready to submit", "Submitted", "Accepted", "Pending"}
    ))
    claim_list = claims or []
    scrubbed = [claim for claim in claim_list if claim.get("Validation Summary")]
    clean = [claim for claim in scrubbed if claim["Validation Summary"].get("errors", 1) == 0]
    net_base = charges - contractual - denied
    return {
        "Gross charges": charges,
        "Expected allowed": total("Allowed_Amount"),
        "Contractual adjustments": contractual,
        "Denied amount": denied,
        "Payer expected": total("Payer_Expected_Payment"),
        "Payer paid": payer_paid,
        "Patient responsibility": patient_resp,
        "Collected at visit": at_visit,
        "Patient payments": patient_paid,
        "Financial assistance": assistance,
        "Outstanding patient A/R": outstanding,
        "Accounts with balance": len(open_rows),
        "Average patient balance": money(outstanding / len(open_rows)) if open_rows else 0.0,
        "Patient A/R over 90 days %": round(100 * over_90 / outstanding, 1) if outstanding else 0.0,
        "Point-of-service collection rate %": round(100 * at_visit / patient_resp, 1) if patient_resp else 0.0,
        "Denial rate %": round(100 * len(denied_accounts) / len(adjudicated), 1) if adjudicated else 0.0,
        "Net collection rate %": round(100 * (payer_paid + patient_paid + at_visit) / net_base, 1) if net_base > 0 else 0.0,
        "Days in A/R": round(total_ar / (recent_charges / 90), 1) if recent_charges else 0.0,
        "Clean claim rate %": round(100 * len(clean) / len(scrubbed), 1) if scrubbed else None,
        "A/R by aging bucket": by_bucket,
    }


KPI_DEFINITIONS = {
    "Gross charges": "Sum of chargemaster charges on the accounts.",
    "Contractual adjustments": "Charges minus payer-allowed amounts (CARC 45), written off per contract.",
    "Patient responsibility": "Copay + deductible + coinsurance the payer assigns to the patient (PR group).",
    "Outstanding patient A/R": "Patient responsibility minus payments at visit, later payments and financial assistance.",
    "Patient A/R over 90 days %": "Share of open patient balances more than 90 days past the statement due date.",
    "Point-of-service collection rate %": "Collected at the visit divided by patient responsibility.",
    "Denial rate %": "Adjudicated encounters with at least one denied line, over all adjudicated encounters.",
    "Net collection rate %": "Payer + patient payments over (charges − contractual adjustments − denials).",
    "Days in A/R": "Open A/R (payer expected on unpaid claims + patient balances) over average daily charges (last 90 days).",
    "Clean claim rate %": "Generated claims that passed scrubbing with no errors, over all scrubbed claims.",
}


def parse_dx_list(value: Any) -> list[str]:
    return [code for code in re.split(r"[;,\s]+", str(value or "").upper()) if code]
