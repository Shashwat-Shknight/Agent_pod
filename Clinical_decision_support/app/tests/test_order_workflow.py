"""Ordered tests become billable when done in clinic; diagnoses coded later still justify them."""

from datetime import date

from frontend import diagnostics as dx
from frontend import rcm
from frontend.views.consultation import coverage_row

RECORD = {"id": "ENC700100001", "ehr_encounter_id": "ENC700100001", "ehr_patient_id": "P1", "mrn": "MRN1"}


def test_point_of_care_and_specimen_tests_are_done_in_clinic_but_studies_are_not():
    assert dx.done_in_clinic(dx.by_key()["ecg"]) and dx.done_in_clinic(dx.by_key()["a1c"]) and dx.done_in_clinic(dx.by_key()["ua"])
    assert not dx.done_in_clinic(dx.by_key()["mpi"]) and not dx.done_in_clinic(dx.by_key()["mri_brain"])


def test_diagnoses_coded_after_ordering_are_linked_at_billing_time():
    order = dx.new_order("a1c", RECORD, linked_dx=[])
    dx.mark_performed(order)
    record = {**RECORD, "diagnostic_orders": [order]}
    assert dx.billing_lines(record)[0]["linked_dx"] == []
    record["code_results"] = {"icd10": [{"ICD-10 Code": "E11.65", "Matched Disease/Injury": "T2DM with hyperglycemia"}]}
    lines = dx.billing_lines(record)
    assert lines[0]["linked_dx"] == ["E11.65"] and lines[-1]["code"] == dx.VENIPUNCTURE_CPT and lines[-1]["linked_dx"] == ["E11.65"]


def test_self_pay_gets_the_uninsured_discount():
    benefits = rcm.plan_benefits({}, date(2026, 11, 28))
    line = rcm.adjudicate([rcm.ClaimLine("99213", "Visit", 315.0, ("I10",))], benefits)["lines"][0]
    assert benefits["payer_class"] == "Self-pay" and line["allowed"] == 189.0 and line["patient"] == 189.0 and line["payer"] == 0


def test_registration_coverage_row_uses_policy_defaults():
    row = coverage_row("P1", {"payer": "Aetna", "plan_type": "POS", "member_id": ""}, 2026)
    assert row["Payer_Name"] == "Aetna" and row["Annual_Deductible_USD"] == 2000 and row["Copay_USD"] == 30
    assert row["Member_ID"].startswith("MBR") and row["Effective_Date"] == "2026-01-01" and row["Status"] == "Active"
