"""RCM persistence: eligibility snapshots, patient accounts and remittance, saved to the EHR in Snowflake.

Sheets written (EHR.SHEETS, one row per encounter):
  Payer_Plan_Benefits  - the 271 benefits snapshot and accumulators before/after adjudication
  Patient_Balance      - the patient account: charges, allowed, adjustments, cost share, payments, aging
  Billing_Claims       - one line per adjudicated CPT (Source = "NuuCare RCM")
  Encounters           - primary diagnosis and status of the visit once it is coded and billed
Claims themselves stay in BILLING.CLAIMS; their JSON carries the submission and remittance history.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from frontend import rcm
from frontend.api_client import HealthcareApiClient
from frontend.ehr_chart import primary_coverage
from frontend.orders_service import service_date
from frontend.payer_gateway import get_gateway

MAX_POINTERS = 4  # 837P allows four diagnosis pointers per service line


def _money(value: Any) -> float:
    return rcm._number(value)


def patient_name(record: dict[str, Any], chart: dict[str, Any] | None) -> str:
    patient = (chart or {}).get("patient") or {}
    return str(patient.get("Legal_Name") or record.get("name") or "")


def diagnosis_codes(record: dict[str, Any]) -> list[str]:
    """Diagnoses that may go on a claim: only those the certified coder accepted, in matcher order."""
    codes = []
    for item in (record.get("code_results") or {}).get("icd10", []) or []:
        code = str((item or {}).get("ICD-10 Code") or "").strip().upper()
        decision = ((record.get("code_decisions") or {}).get(f"ICD:{code}") or {}).get("status")
        if code and decision == "Accepted" and code not in codes:
            codes.append(code)
    return codes


def benefit_rows(client: HealthcareApiClient | None = None) -> list[dict[str, Any]]:
    try:
        return (client or HealthcareApiClient()).get_sheet("Payer_Plan_Benefits").get("records", [])
    except Exception:  # noqa: BLE001 - benefits fall back to chart history when the sheet is unreachable
        return []


def encounter_benefits(record: dict[str, Any], chart: dict[str, Any] | None,
                       rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    coverage = primary_coverage(chart) if chart else {}
    dos = service_date(record)
    encounter_id = str(record.get("ehr_encounter_id") or record.get("id") or "")
    rows = [row for row in (rows or []) if str(row.get("Encounter_ID")) != encounter_id]
    accumulators = rcm.accumulators_from(rows, str(coverage.get("Member_ID") or ""), dos.year)
    prior = 0.0 if accumulators else rcm.history_liability(
        chart, str(record.get("ehr_patient_id") or ""), dos.year, exclude_encounter=encounter_id)
    return get_gateway().eligibility(coverage, dos, prior_liability=prior, accumulators=accumulators)


def claim_lines(priced_rows: list[dict[str, Any]], order_lines: list[dict[str, Any]], dx_codes: list[str]) -> list[dict[str, Any]]:
    """Serializable claim lines with diagnosis pointers and authorization numbers.

    Ordered tests point at the diagnoses that justified them; the visit and other services point at
    the encounter's diagnoses (up to four, primary first).
    """
    by_code = {line["code"]: line for line in order_lines}
    lines = []
    for row in priced_rows:
        code = str(row.get("CPT/HCPCS Code") or "").strip()
        order = by_code.get(code) or {}
        # A pointer must reference a diagnosis on the claim (837 HI segment): problem-list codes that
        # were not addressed in this visit cannot be billed, so they never become pointers.
        pointers = [code_ for code_ in order.get("linked_dx") or [] if code_ in dx_codes] or dx_codes
        quantity = rcm._number(row.get("Quantity"), 1.0) or 1.0
        lines.append({
            "code": code,
            "description": str(row.get("Service Description") or order.get("service") or ""),
            "charge": _money(row.get("Gross Charge")),
            "units": quantity,
            "dx": list(dict.fromkeys(pointers))[:MAX_POINTERS],
            "auth_required": bool(order.get("prior_auth_required")),
            "auth_number": str(order.get("auth_number") or ""),
            "order_id": str(order.get("order_id") or ""),
        })
    return lines


def adjudicate_lines(lines: list[dict[str, Any]], benefits: dict[str, Any], dx_codes: list[str]) -> dict[str, Any]:
    claim = [rcm.ClaimLine(line["code"], line["description"], line["charge"], tuple(line.get("dx") or ()),
                           bool(line.get("auth_required")), str(line.get("auth_number") or ""), line.get("units") or 1.0)
             for line in lines]
    return get_gateway().remittance(claim, benefits, primary_dx=dx_codes[0] if dx_codes else "")


def save_send_to_rcm(record: dict[str, Any], chart: dict[str, Any] | None, benefits: dict[str, Any],
                     adjudication: dict[str, Any], *, collected_at_visit: float = 0.0,
                     client: HealthcareApiClient | None = None) -> dict[str, Any]:
    """Persist the benefits snapshot, open the patient account and stamp the encounter's diagnosis."""
    client = client or HealthcareApiClient()
    patient_id = str(record.get("ehr_patient_id") or "")
    encounter_id = str(record.get("ehr_encounter_id") or record.get("id") or "")
    if not patient_id or not encounter_id:
        raise ValueError("The encounter must be linked to an EHR patient and encounter before it is billed.")
    mrn = str(record.get("mrn") or ((chart or {}).get("patient") or {}).get("MRN") or "")
    name = patient_name(record, chart)
    coverage = primary_coverage(chart) if chart else {}
    guarantor = rcm.guarantor_name(name, coverage)
    account = rcm.new_account(
        patient_id=patient_id, mrn=mrn, encounter_id=encounter_id, patient_name=name,
        service_date=service_date(record), benefits=benefits, adjudication=adjudication, guarantor=guarantor,
        collected_at_visit=collected_at_visit,
    )
    existing = next((row for row in client.get_sheet("Patient_Balance").get("records", [])
                     if str(row.get("Account_ID")) == encounter_id), None)
    if existing and str(existing.get("Claim_Status") or "") not in {"Ready to submit", "Rejected", "Draft", ""}:
        raise ValueError(
            f"Account {encounter_id} is already {existing.get('Claim_Status')} with the payer; "
            "a corrected claim is needed to change it, so the posted account was left unchanged."
        )
    if existing:  # re-sending before submission keeps payments already posted
        for field in ("Patient_Payments", "Financial_Assistance", "Claim_IDs"):
            account[field] = existing.get(field) or account.get(field)
        if not collected_at_visit:
            account["Patient_Paid_At_Discharge"] = existing.get("Patient_Paid_At_Discharge") or 0.0
        account = rcm.refresh_account(account)
    client.upsert_patient_records(patient_id, "Patient_Balance", [account], "Account_ID")
    client.upsert_patient_records(patient_id, "Payer_Plan_Benefits", [rcm.benefits_row(
        benefits, patient_id=patient_id, mrn=mrn, encounter_id=encounter_id, guarantor=guarantor, account=account)],
        "Benefit_Key")
    dx_codes = diagnosis_codes(record)
    primary = next((item for item in (record.get("code_results") or {}).get("icd10", [])
                    if str(item.get("ICD-10 Code") or "").upper() == (dx_codes[0] if dx_codes else "")), {})
    client.upsert_patient_records(patient_id, "Encounters", [{
        "Encounter_ID": encounter_id,
        "MRN": mrn,
        "Status": "Coded - sent to billing",
        "Primary_Diagnosis_Code": dx_codes[0] if dx_codes else None,
        "Primary_Diagnosis": primary.get("Matched Disease/Injury") or None,
        "Secondary_Diagnosis_Codes": ";".join(dx_codes[1:]),
    }], "Encounter_ID")
    return account


def load_accounts(include_history: bool = True, client: HealthcareApiClient | None = None,
                  today: date | None = None) -> list[dict[str, Any]]:
    """Patient accounts from Patient_Balance, plus accounts derived from the chart's claim history."""
    client = client or HealthcareApiClient()
    today = today or date.today()
    accounts = [rcm.refresh_account(row, today) for row in client.get_sheet("Patient_Balance").get("records", [])]
    if include_history:
        known = {row.get("Encounter_ID") for row in accounts}
        patients = {str(row.get("Patient_ID")): row for row in client.get_sheet("Patient_Master").get("records", [])}
        claims = client.get_sheet("Billing_Claims").get("records", [])
        accounts += [row for row in rcm.accounts_from_chart_claims(claims, patients, today) if row.get("Encounter_ID") not in known]
    return accounts


def save_account(account: dict[str, Any], client: HealthcareApiClient | None = None) -> dict[str, Any]:
    if account.get("Source") != "NuuCare RCM":
        raise ValueError("Accounts from the chart's claim history are read-only here.")
    client = client or HealthcareApiClient()
    saved = client.upsert_patient_records(str(account["Patient_ID"]), "Patient_Balance", [account], "Account_ID")
    return saved[0] if saved else account


def submit_claims(account: dict[str, Any], claims: list[dict[str, Any]], *, client: HealthcareApiClient | None = None,
                  now: datetime | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Send the encounter's claims to the clearinghouse (837); 277CA acceptance is checked on the spot."""
    now = now or datetime.now()
    if str(account.get("Claim_Status") or "") not in {"Ready to submit", "Rejected", "Draft", ""}:
        raise ValueError(f"Account {account.get('Account_ID')} is already {account.get('Claim_Status')}; it cannot be submitted again.")
    if not claims:
        raise ValueError("Generate the claims for this encounter before submitting them.")
    gateway = get_gateway()
    updated = []
    rejected = []
    for claim in claims:
        accepted, problems = gateway.acknowledge(claim)
        history = list(claim.get("Lifecycle") or [])
        history.append({"at": now.strftime("%Y-%m-%d %H:%M"), "event": "Submitted (837 to clearinghouse)"})
        if not accepted:
            status = "Rejected"
            history.append({"at": now.strftime("%Y-%m-%d %H:%M"), "event": "277CA rejected: missing " + ", ".join(problems)})
            rejected.append(claim["Claim ID"])
        else:
            status = "Accepted"
            history.append({"at": now.strftime("%Y-%m-%d %H:%M"), "event": "277CA accepted by payer"})
        updated.append({**claim, "Status": status, "Lifecycle": history, "Submitted At": now.strftime("%Y-%m-%d %H:%M")})
    row = dict(account)
    row["Claim_IDs"] = ";".join(claim["Claim ID"] for claim in claims)
    row["Claim_Status"] = "Rejected" if rejected else "Accepted"
    row = rcm.refresh_account(row)
    save_account(row, client)
    return row, updated


def post_remittance(record: dict[str, Any], chart: dict[str, Any] | None, account: dict[str, Any],
                    claims: list[dict[str, Any]], *, client: HealthcareApiClient | None = None,
                    today: date | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Apply the payer's 835 for every claim of the encounter, move accumulators, statement the patient."""
    client = client or HealthcareApiClient()
    today = today or date.today()
    if str(account.get("Claim_Status") or "") != "Accepted":
        # Idempotent: a remittance is posted once, after the payer accepted the claim.
        raise ValueError(f"Account {account.get('Account_ID')} is {account.get('Claim_Status')}; a remittance needs an accepted claim.")
    estimate = record.get("bill_estimate") or {}
    lines = estimate.get("claim_lines") or []
    if not lines:
        raise ValueError("This encounter was sent to RCM before line-level adjudication existed; send it to RCM again.")
    dx_codes = diagnosis_codes(record)
    rows = benefit_rows(client)
    benefits = encounter_benefits(record, chart, rows)
    adjudication = adjudicate_lines(lines, benefits, dx_codes)
    posted = rcm.post_remittance(account, adjudication, today)
    save_account(posted, client)
    patient_id = str(account["Patient_ID"])
    encounter_id = str(account["Encounter_ID"])
    coverage = primary_coverage(chart) if chart else {}
    snapshot = rcm.benefits_row(benefits, patient_id=patient_id, mrn=str(account.get("MRN") or ""),
                                encounter_id=encounter_id, guarantor=str(account.get("Guarantor") or ""), account=posted)
    snapshot["Deductible_Met_After"] = rcm.money(benefits["deductible_met"] + adjudication["totals"]["deductible"])
    snapshot["OOP_Met_After"] = rcm.money(benefits["oop_met"] + adjudication["totals"]["patient"])
    client.upsert_patient_records(patient_id, "Payer_Plan_Benefits", [snapshot], "Benefit_Key")

    by_code = {line["code"]: line for line in adjudication["lines"]}
    updated_claims, claim_rows = [], []
    for claim in claims:
        codes = [code for code in claim.get("CPT/HCPCS Codes") or []]
        claim_lines_ = [by_code[code] for code in codes if code in by_code]
        paid = rcm.money(sum(line["payer"] for line in claim_lines_))
        denied = [line for line in claim_lines_ if line["denied"]]
        status = "Denied" if claim_lines_ and len(denied) == len(claim_lines_) else "Partially denied" if denied else "Paid"
        history = list(claim.get("Lifecycle") or [])
        history.append({"at": today.isoformat(), "event": f"835 remittance posted: {status}, payer paid ${paid:,.2f}"})
        updated_claims.append({
            **claim, "Status": status, "Lifecycle": history,
            "Remittance": {
                "Payer Claim Control Number": rcm.control_number(claim["Claim ID"]),
                "Adjudication Date": today.isoformat(),
                "Paid Amount": paid,
                "Patient Responsibility": rcm.money(sum(line["patient"] for line in claim_lines_)),
                "Lines": claim_lines_,
            },
        })
        for line in claim_lines_:
            claim_rows.append({
                "Claim_ID": f"{claim['Claim ID']}:{line['code']}",
                "Encounter_ID": encounter_id,
                "MRN": account.get("MRN") or "",
                "Service_Date": account.get("Service_Date") or "",
                "CPT_HCPCS": line["code"],
                "Description": line["description"],
                "Units": 1,
                "Charge_USD": line["charge"],
                "Allowed_USD": line["allowed"],
                "Contractual_Adjustment_USD": rcm.money(line["contractual"] + line["denied"]),
                "Payment_USD": line["payer"],
                "Patient_Balance_USD": line["patient"],
                "Payer": coverage.get("Payer_Name") or "",
                "Claim_Status": "Denied" if line["denied"] else "Paid",
                "Adjustment_Codes": ";".join(line["carc"]),
                "Diagnosis_Pointers": ";".join(line["dx"]),
                "Authorization_Number": line.get("auth_number") or "",
                "Currency": "USD",
                "Source": "NuuCare RCM",
            })
    if claim_rows:
        client.upsert_patient_records(patient_id, "Billing_Claims", claim_rows, "Claim_ID")
    client.upsert_patient_records(patient_id, "Encounters", [{"Encounter_ID": encounter_id, "Status": f"Billed - {posted['Claim_Status']}"}],
                                  "Encounter_ID")
    return posted, updated_claims, adjudication
