from datetime import date

import pytest

from frontend import rcm
from frontend.rcm import ClaimLine

DOS = date(2026, 11, 20)
PPO = {
    "Payer_Name": "UnitedHealthcare Demo PPO", "Plan_Type": "PPO", "Member_ID": "MBR800001", "Status": "Active",
    "Effective_Date": "2026-01-01T00:00:00", "Termination_Date": "2026-12-31T00:00:00",
    "Annual_Deductible_USD": 1500, "Copay_USD": 20, "Relationship_to_Subscriber": "Self",
}


def test_payer_class_reads_payer_name_before_plan_type():
    assert rcm.payer_class(PPO) == "Commercial"
    assert rcm.payer_class({"Payer_Name": "Medicare Demo", "Plan_Type": "PPO"}) == "Medicare"
    assert rcm.payer_class({"Payer_Name": "Medicaid Demo", "Plan_Type": "HMO"}) == "Medicaid"
    assert rcm.payer_class({"Payer_Name": "Cigna Demo Open Access", "Plan_Type": "Medicare"}) == "Medicare Advantage"
    assert rcm.payer_class({}) == "Self-pay"


def test_eligibility_checks_status_and_coverage_dates():
    assert rcm.eligibility(PPO, DOS)["status"] == "Active"
    assert rcm.eligibility({**PPO, "Termination_Date": "2026-06-30"}, DOS)["status"] == "Inactive"
    assert rcm.eligibility({**PPO, "Status": "Terminated"}, DOS)["status"] == "Inactive"
    assert rcm.eligibility({}, DOS)["status"] == "No coverage"


def test_commercial_benefits_use_plan_terms_and_seed_accumulators_from_history():
    benefits = rcm.plan_benefits(PPO, DOS, prior_liability=400.0)
    assert benefits["copay"] == 20 and benefits["annual_deductible"] == 1500
    assert benefits["deductible_met"] == 400 and benefits["remaining_deductible"] == 1100
    assert benefits["coinsurance"] == 0.20 and benefits["oop_max"] == 6000 and benefits["oop_remaining"] == 5600
    assert benefits["allowed_rate"] == 0.58


def test_office_visit_takes_copay_and_labs_take_deductible_then_coinsurance():
    benefits = rcm.plan_benefits(PPO, DOS, prior_liability=1450.0)  # $50 of deductible left
    result = rcm.adjudicate([
        ClaimLine("99214", "Visit", 410.0, ("E11.9",)),
        ClaimLine("83036", "HbA1c", 110.0, ("E11.9",)),
        ClaimLine("80061", "Lipid panel", 215.0, ("E11.9",)),
    ], benefits, primary_dx="E11.9")
    visit, a1c, lipid = result["lines"]
    assert visit["allowed"] == 237.80 and visit["copay"] == 20 and visit["deductible"] == 0
    assert visit["carc"] == ["CO-45", "PR-3"]
    assert a1c["allowed"] == 63.80 and a1c["deductible"] == 50 and a1c["coinsurance"] == 2.76
    assert lipid["deductible"] == 0 and lipid["coinsurance"] == 24.94
    totals = result["totals"]
    assert totals["patient"] == pytest.approx(20 + 50 + 2.76 + 24.94)
    assert totals["payer"] == pytest.approx(totals["allowed"] - totals["patient"])
    assert totals["contractual"] == pytest.approx(totals["charge"] - totals["allowed"])
    assert result["remaining_deductible_after"] == 0
    assert result["status"] == "Paid"


def test_out_of_pocket_maximum_caps_patient_share():
    benefits = rcm.plan_benefits(PPO, DOS, accumulators={"deductible_met": 1500, "oop_met": 5990})
    result = rcm.adjudicate([ClaimLine("93306", "Echo", 1550.0, ("I50.9",), True, "AUTH1")], benefits)
    assert result["lines"][0]["patient"] == 10.0
    assert result["oop_remaining_after"] == 0


def test_missing_authorization_and_unsupported_diagnosis_are_denied_not_billed_to_patient():
    benefits = rcm.plan_benefits(PPO, DOS)
    result = rcm.adjudicate([
        ClaimLine("70551", "MRI brain", 3250.0, ("G43.909",), auth_required=True),
        ClaimLine("83036", "HbA1c", 110.0, ("J45.909",)),
        ClaimLine("99213", "Visit", 315.0, ("J45.909",)),
    ], benefits)
    mri, a1c, visit = result["lines"]
    assert mri["carc"] == ["CO-197"] and mri["denied"] == 3250 and mri["patient"] == 0
    assert a1c["carc"] == ["CO-50"] and a1c["patient"] == 0
    assert visit["payer"] > 0
    assert result["status"] == "Partially denied"


def test_inactive_coverage_denies_every_line():
    benefits = rcm.plan_benefits({**PPO, "Termination_Date": "2026-03-31"}, DOS)
    result = rcm.adjudicate([ClaimLine("99213", "Visit", 315.0, ("I10",))], benefits)
    assert result["lines"][0]["carc"] == ["CO-27"] and result["status"] == "Denied"


def test_preventive_services_have_no_cost_share():
    benefits = rcm.plan_benefits(PPO, DOS)
    result = rcm.adjudicate([ClaimLine("77067", "Screening mammogram", 535.0, ("Z12.31",))], benefits)
    assert result["lines"][0]["kind"] == "Preventive" and result["lines"][0]["patient"] == 0


def test_original_medicare_has_no_copay_and_no_out_of_pocket_cap():
    benefits = rcm.plan_benefits({**PPO, "Payer_Name": "Medicare Demo", "Annual_Deductible_USD": 257}, DOS)
    assert benefits["copay"] == 0 and benefits["oop_max"] is None
    line = rcm.adjudicate([ClaimLine("99213", "Visit", 315.0, ("I10",))], benefits)["lines"][0]
    assert line["copay"] == 0 and line["deductible"] == pytest.approx(min(257, 315 * 0.38))


def test_medicaid_copay_is_nominal():
    benefits = rcm.plan_benefits({**PPO, "Payer_Name": "Medicaid Demo", "Copay_USD": 30}, DOS)
    assert benefits["copay"] == 4 and benefits["annual_deductible"] == 0


def _account(adjudication):
    return rcm.new_account(
        patient_id="P100001", mrn="MRN500001", encounter_id="ENC700100001", patient_name="Olivia Johnson",
        service_date=DOS, benefits=rcm.plan_benefits(PPO, DOS), adjudication=adjudication, guarantor="Olivia Johnson",
        collected_at_visit=20.0,
    )


def test_account_lifecycle_statement_aging_payment_and_close():
    benefits = rcm.plan_benefits(PPO, DOS)
    adjudication = rcm.adjudicate([ClaimLine("99214", "Visit", 410.0, ("E11.9",)), ClaimLine("83036", "HbA1c", 110.0, ("E11.9",))], benefits)
    account = _account(adjudication)
    assert account["Aging_Bucket"] == "Not yet billed" and account["Recommended_Action"] == "Submit claim to payer"

    account = rcm.post_remittance({**account, "Claim_Status": "Submitted"}, adjudication, today=date(2026, 11, 25))
    assert account["Payer_Paid"] == adjudication["totals"]["payer"]
    assert account["Statement_Date"] == "2026-11-27" and account["Payment_Due_Date"] == "2026-12-27"
    balance = round(adjudication["totals"]["patient"] - 20, 2)
    assert account["Delta_Patient_Balance"] == balance and account["Aging_Bucket"] == "Current"

    late = rcm.refresh_account(account, today=date(2027, 2, 10))
    assert late["Days_Past_Due"] == 45 and late["Aging_Bucket"] == "31-60"
    assert late["Follow_up_Priority"] == "Medium" and late["Recommended_Action"].startswith("Call patient")

    paid = rcm.apply_payment(late, balance, today=date(2027, 2, 10))
    assert paid["Delta_Patient_Balance"] == 0 and paid["Recommended_Action"] == "Close — no patient balance"
    refund = rcm.apply_payment(paid, 5, today=date(2027, 2, 10))
    assert refund["Aging_Bucket"] == "Credit balance" and refund["Recommended_Action"] == "Refund patient credit balance"


def test_ninety_plus_respects_501r_notice_period():
    account = {"Patient_Responsibility": 500, "Claim_Status": "Paid", "Statement_Date": "2026-06-01", "Payment_Due_Date": "2026-07-01"}
    row = rcm.refresh_account(account, today=date(2026, 10, 15))
    assert row["Aging_Bucket"] == "90+" and row["Follow_up_Priority"] == "High"
    assert "501(r)" in row["Recommended_Action"]


def test_denied_account_routes_to_denial_work():
    benefits = rcm.plan_benefits(PPO, DOS)
    adjudication = rcm.adjudicate([ClaimLine("70551", "MRI", 3250.0, ("G43.909",), auth_required=True)], benefits)
    account = rcm.post_remittance(_account(adjudication), adjudication, today=DOS)
    assert account["Claim_Status"] == "Denied" and account["Denial_Reasons"] == "CO-197"
    assert "retro-authorization" in account["Recommended_Action"] and account["Follow_up_Priority"] == "High"


def test_accounts_from_chart_claims_and_kpis():
    claims = [
        {"Claim_ID": "CLM1", "Patient_ID": "P1", "Encounter_ID": "E1", "Service_Date": "2026-09-20", "Charge_USD": 185,
         "Contractual_Adjustment_USD": 40, "Payment_USD": 100, "Payer": "UHC", "Claim_Status": "Paid"},
        {"Claim_ID": "CLM2", "Patient_ID": "P2", "Encounter_ID": "E2", "Service_Date": "2026-11-01", "Charge_USD": 300,
         "Contractual_Adjustment_USD": 0, "Payment_USD": 0, "Payer": "UHC", "Claim_Status": "Submitted"},
    ]
    accounts = rcm.accounts_from_chart_claims(claims, {"P1": {"MRN": "MRN1", "Legal_Name": "A B"}}, date(2026, 11, 28))
    first = next(row for row in accounts if row["Encounter_ID"] == "E1")
    assert first["Patient_Responsibility"] == 45 and first["Statement_Date"] == "2026-10-06"
    assert first["Aging_Bucket"] == "1-30" and first["Days_Past_Due"] == 23 and first["MRN"] == "MRN1"
    second = next(row for row in accounts if row["Encounter_ID"] == "E2")
    assert second["Aging_Bucket"] == "With payer"
    summary = rcm.kpis(accounts, today=date(2026, 11, 28))
    assert summary["Outstanding patient A/R"] == 45 and summary["Accounts with balance"] == 1
    assert summary["A/R by aging bucket"]["1-30"] == 45


def test_claim_line_pointers_only_reference_diagnoses_on_the_claim():
    from frontend import rcm_service

    priced = [{"CPT/HCPCS Code": "83036", "Gross Charge": "$110.00", "Quantity": 1, "Service Description": "HbA1c"},
              {"CPT/HCPCS Code": "99214", "Gross Charge": "$410.00", "Quantity": 1, "Service Description": "Visit"}]
    orders = [{"code": "83036", "linked_dx": ["E11.9", "E11.65"], "auth_number": "", "prior_auth_required": False, "order_id": "ORD1"}]
    lines = rcm_service.claim_lines(priced, orders, ["E11.65", "G43.109"])
    assert lines[0]["dx"] == ["E11.65"] and lines[0]["order_id"] == "ORD1" and lines[0]["charge"] == 110.0
    assert lines[1]["dx"] == ["E11.65", "G43.109"]


def test_benefits_row_and_accumulators_round_trip():
    benefits = rcm.plan_benefits(PPO, DOS)
    row = rcm.benefits_row(benefits, patient_id="P1", mrn="MRN1", encounter_id="ENC700100001", guarantor="A B")
    assert row["Member_ID"] == "MBR800001" and row["Eligibility_Status"] == "Active"
    assert rcm.accumulators_from([row], "MBR800001", 2026) is None
    row.update(Deductible_Met_After=300, OOP_Met_After=320, Insurance_Adjudication_Date="2026-11-25")
    assert rcm.accumulators_from([row], "MBR800001", 2026) == {"deductible_met": 300, "oop_met": 320}
