from datetime import date, datetime

import pytest

from frontend import diagnostics as dx
from frontend import prior_auth as pa

TODAY = date(2026, 11, 28)
CHART = {"records": {
    "Problems": [
        {"ICD_10_CM": "E11.9", "Problem_Name": "Type 2 diabetes mellitus", "Clinical_Status": "Active"},
        {"ICD_10_CM": "I10", "Problem_Name": "Essential hypertension", "Clinical_Status": "Active"},
    ],
    "Labs": [
        {"LOINC_Code": "4548-4", "Test_Name": "Hemoglobin A1c", "Result_Date": "2026-10-01T00:00:00", "Result": 7.9, "Unit": "%"},
        {"LOINC_Code": "2093-3", "Test_Name": "Total cholesterol", "Result_Date": "2026-03-01T00:00:00", "Result": 210, "Unit": "mg/dL"},
    ],
}}
RECORD = {
    "id": "ENC700100001", "ehr_encounter_id": "ENC700100001", "ehr_patient_id": "P100001", "mrn": "MRN500001",
    "doctor": "Dr. Elena Ruiz",
    "summary": "SUBJECTIVE\nTired.\nASSESSMENT\nType 2 diabetes, uncontrolled. Patient denies chest pain.\nPLAN\nIncrease metformin.",
    "recommendations": "1. **Repeat HbA1c in 3 months**\n2. **Order a lipid panel and urine albumin-to-creatinine ratio**",
    "code_results": {"icd10": [{"ICD-10 Code": "E11.65", "Matched Disease/Injury": "Type 2 diabetes with hyperglycemia"}]},
}


def _by_key(items):
    return {item["test_key"]: item for item in items}


def test_cds_and_plan_mentions_become_suggestions_with_reasons():
    suggestions = _by_key(dx.suggest_tests(RECORD, CHART, TODAY))
    assert suggestions["lipid"]["preselect"] and "CDS recommendation" in suggestions["lipid"]["reasons"][0]
    assert suggestions["uacr"]["preselect"]
    assert suggestions["lipid"]["linked_dx"] == ["E11.65", "E11.9", "I10"]


def test_frequency_guard_marks_recent_tests_not_due():
    suggestions = _by_key(dx.suggest_tests(RECORD, CHART, TODAY))
    a1c = suggestions["a1c"]
    assert not a1c["due"]["due"] and not a1c["preselect"]
    assert "repeat from 30 Dec 2026" in a1c["due"]["label"]
    assert suggestions["lipid"]["due"]["due"]  # total cholesterol in March counts as the last panel


def test_negated_symptoms_do_not_trigger_tests():
    suggestions = _by_key(dx.suggest_tests(RECORD, CHART, TODAY))
    assert "troponin" not in suggestions
    ecg = suggestions.get("ecg")
    assert ecg is None or not ecg["preselect"]


def test_diagnosis_alone_does_not_preselect_acute_tests():
    record = {**RECORD, "recommendations": "", "summary": "ASSESSMENT\nHypertension follow-up."}
    suggestions = _by_key(dx.suggest_tests(record, {"records": {}}, TODAY))
    assert not suggestions.get("ecg", {}).get("preselect", False)


def test_already_ordered_tests_are_not_suggested_again():
    record = {**RECORD, "diagnostic_orders": [dx.new_order("lipid", RECORD)]}
    assert "lipid" not in _by_key(dx.suggest_tests(record, CHART, TODAY))


def test_order_lifecycle_and_billing_states():
    order = dx.new_order("a1c", RECORD, linked_dx=["E11.65"], ordered_by="dr", now=datetime(2026, 11, 28, 9, 0))
    order["order_id"] = "ORD30000001"
    record = {**RECORD, "diagnostic_orders": [order]}
    lines = dx.billing_lines(record)
    assert [(line["code"], line["state"]) for line in lines] == [("83036", "PENDING")]

    dx.record_result(order, "8.4", now=datetime(2026, 11, 28, 9, 30))
    assert order["status"] == dx.RESULTED and order["result"]["flag"] == "High"
    lines = dx.billing_lines(record)
    assert [(line["code"], line["state"]) for line in lines] == [("83036", "BILLABLE"), ("36415", "BILLABLE")]
    assert "8.4 %" in lines[0]["reason"] and lines[1]["linked_dx"] == ["E11.65"]
    with pytest.raises(ValueError):
        dx.cancel_order(order)


def test_two_blood_tests_add_one_venipuncture():
    first, second = dx.new_order("a1c", RECORD), dx.new_order("lipid", RECORD)
    dx.mark_performed(first)
    dx.mark_performed(second)
    codes = [line["code"] for line in dx.billing_lines({**RECORD, "diagnostic_orders": [first, second]})]
    assert codes.count("36415") == 1


def test_unapproved_authorization_holds_a_performed_study():
    order = dx.new_order("mri_brain", RECORD, linked_dx=["G43.909"])
    order["prior_auth"] = {"required": True, "status": pa.PENDED}
    dx.mark_performed(order)
    line = dx.billing_lines({**RECORD, "diagnostic_orders": [order]})[0]
    assert line["state"] == "HELD" and "CARC 197" in line["reason"]
    order["prior_auth"] = {"required": True, "status": pa.APPROVED, "auth_number": "AUTH1"}
    assert dx.billing_lines({**RECORD, "diagnostic_orders": [order]})[0]["state"] == "BILLABLE"


def test_plan_line_never_shows_billing_codes():
    order = dx.new_order("a1c", RECORD)
    suggestion = _by_key(dx.suggest_tests({**RECORD, "diagnostic_orders": []}, CHART, TODAY))["a1c"]
    line = dx.plan_line(order, suggestion)
    assert line.startswith("Order: Hemoglobin A1c") and "83036" not in line and "4548-4" not in line


def test_ehr_rows_link_patient_mrn_encounter_and_order():
    order = dx.new_order("a1c", RECORD, linked_dx=["E11.65"])
    order["order_id"] = "ORD30000001"
    dx.record_result(order, "8.4")
    row = dx.ehr_order_row(order, RECORD)
    assert row["MRN"] == "MRN500001" and row["Encounter_ID"] == "ENC700100001" and row["CPT_HCPCS"] == "83036"
    lab = dx.ehr_lab_row(order, RECORD)
    assert lab["LOINC_Code"] == "4548-4" and lab["Order_ID"] == "ORD30000001" and lab["Flag"] == "High"


def test_result_flags():
    assert dx.result_flag(dx.BY_KEY["tsh"], "0.2") == "Low"
    assert dx.result_flag(dx.BY_KEY["strep"], "Negative") == "Normal"
    assert dx.result_flag(dx.BY_KEY["ecg"], "Atrial fibrillation") == "Abnormal"
    with pytest.raises(ValueError):
        dx.record_result(dx.new_order("tsh", RECORD), "  ")


COMMERCIAL = {"Payer_Name": "Aetna Demo Choice POS", "Plan_Type": "POS", "Member_ID": "MBR1"}


def test_prior_auth_requirement_depends_on_service_and_payer():
    assert pa.requirement("70551", COMMERCIAL)["required"]
    assert not pa.requirement("70551", {"Payer_Name": "Medicare Demo"})["required"]
    assert not pa.requirement("83036", COMMERCIAL)["required"]
    assert pa.requirement("93306", COMMERCIAL)["required"]


def test_prior_auth_submission_approves_supported_and_pends_unsupported_requests():
    order = dx.new_order("mri_brain", RECORD, linked_dx=["G43.909"])
    order["order_id"] = "ORD30000002"
    request = pa.build_request(order, RECORD, COMMERCIAL, "dr")
    request["PA_ID"] = "PA50000001"
    approved = pa.submit(request, now=datetime(2026, 11, 28, 10, 0))
    assert approved["Status"] == pa.APPROVED and approved["Auth_Number"].startswith("AUTH")
    assert approved["Valid_Through"] == "2027-01-27" and pa.is_valid(approved, date(2026, 12, 1))
    assert "uncontrolled" in approved["Clinical_Justification"].lower()

    order["linked_dx"] = []
    pended = pa.submit(pa.build_request(order, RECORD, COMMERCIAL, "dr"))
    assert pended["Status"] == pa.PENDED and not pended["Auth_Number"]
    with pytest.raises(ValueError):
        pa.record_decision(pended, pa.APPROVED)
    assert pa.record_decision(pended, pa.APPROVED, auth_number="UHC-77")["Auth_Number"] == "UHC-77"
