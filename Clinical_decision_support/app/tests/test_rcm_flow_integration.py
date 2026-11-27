"""Whole revenue-cycle flow through the real API on a temporary JSON EHR store:
order -> prior auth -> result -> eligibility -> expected adjudication -> account -> 837 -> 835 -> payment."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.services.ehr_json_service as ehr
from app.main import app
from frontend import diagnostics as dx
from frontend import orders_service, rcm, rcm_service
from frontend.api_client import HealthcareApiClient
from frontend.encounter_pipeline import build_estimated_hospital_bill


class _TestClientApi(HealthcareApiClient):
    def __init__(self):
        super().__init__()
        self._client = TestClient(app)

    def _request(self, method, endpoint, *, json_body=None, params=None):
        response = self._client.request(method, endpoint, json=json_body, params=params)
        response.raise_for_status()
        return response.json()


@pytest.fixture
def api(tmp_path, monkeypatch):
    payload = {"format": "nuucare-workbook-json-v1", "sheets": {
        "Patient_Master": {"headers": ["Patient_ID", "MRN", "Legal_Name", "DOB"], "records": [
            {"Patient_ID": "P100001", "MRN": "MRN500001", "Legal_Name": "Olivia Johnson", "DOB": "1960-01-01"}]},
        "Insurance": {"headers": ["Coverage_ID", "Patient_ID", "Priority", "Payer_Name", "Member_ID", "Plan_Type", "Status",
                                  "Effective_Date", "Termination_Date", "Annual_Deductible_USD", "Copay_USD",
                                  "Relationship_to_Subscriber"], "records": [
            {"Coverage_ID": "COV1", "Patient_ID": "P100001", "Priority": "Primary", "Payer_Name": "Aetna Demo Choice POS",
             "Member_ID": "MBR1", "Plan_Type": "POS", "Status": "Active", "Effective_Date": "2026-01-01",
             "Termination_Date": "2026-12-31", "Annual_Deductible_USD": 500, "Copay_USD": 25, "Relationship_to_Subscriber": "Self"}]},
        "Problems": {"headers": ["Problem_ID", "Patient_ID", "ICD_10_CM", "Problem_Name", "Clinical_Status"], "records": [
            {"Problem_ID": "PRB1", "Patient_ID": "P100001", "ICD_10_CM": "E11.9", "Problem_Name": "Type 2 diabetes", "Clinical_Status": "Active"}]},
        "Encounters": {"headers": ["Encounter_ID", "Patient_ID", "Date", "Status"], "records": []},
        "Billing_Claims": {"headers": ["Claim_ID", "Patient_ID", "Encounter_ID", "Service_Date", "Charge_USD",
                                       "Contractual_Adjustment_USD", "Payment_USD", "Claim_Status"], "records": [
            {"Claim_ID": "CLM00001", "Patient_ID": "P100001", "Encounter_ID": "ENC0011", "Service_Date": "2026-03-02",
             "Charge_USD": 300, "Contractual_Adjustment_USD": 100, "Payment_USD": 120, "Claim_Status": "Paid"}]},
    }}
    monkeypatch.setattr(ehr, "JSON_PATH", tmp_path / "ehr.json")
    ehr._atomic_write(payload)
    return _TestClientApi()


def test_order_to_payment_flow_persists_every_step(api):
    encounter = api.create_patient_record("P100001", "Encounters", {"Date": "2026-11-20", "MRN": "MRN500001"})
    assert encounter["Encounter_ID"] == "ENC700100001"
    record = {
        "id": encounter["Encounter_ID"], "ehr_encounter_id": encounter["Encounter_ID"], "ehr_patient_id": "P100001",
        "mrn": "MRN500001", "name": "Olivia Johnson", "doctor": "Dr. Ruiz", "date": "20/11/2026",
        "summary": "ASSESSMENT\nType 2 diabetes with hyperglycemia. New migraine with aura.\nPLAN\nMetformin.",
        "recommendations": "1. **Check HbA1c**\n2. **MRI brain for new migraine with aura**",
        "code_results": {"icd10": [{"ICD-10 Code": "E11.65", "Matched Disease/Injury": "Type 2 diabetes with hyperglycemia"},
                                   {"ICD-10 Code": "G43.109", "Matched Disease/Injury": "Migraine with aura"}]},
    }
    chart = api.get_patient_chart("P100001")
    suggestions = {item["test_key"]: item for item in dx.suggest_tests(record, chart, date(2026, 11, 20))}
    created, plan = orders_service.place_orders(
        record, chart, [{"test_key": key, "suggestion": suggestions[key]} for key in ("a1c", "mri_brain")],
        user="tester", plan_text=record["summary"], client=api)
    a1c, mri = created
    assert a1c["order_id"] == "ORD30000001" and mri["order_id"] == "ORD30000002"
    assert mri["prior_auth"]["status"] == "Approved" and mri["prior_auth"]["pa_id"] == "PA50000001"
    assert "Order: Hemoglobin A1c" in plan and "83036" not in plan

    orders_service.update_order(record, a1c["order_id"], "result", "8.6", api)
    orders_service.update_order(record, mri["order_id"], "perform", None, api)
    assert a1c.get("lab_id") == "LAB40000001"
    ehr_orders = {row["Order_ID"]: row for row in api.get_sheet("Orders_Procedures")["records"]}
    assert ehr_orders["ORD30000001"]["Status"] == "Resulted" and ehr_orders["ORD30000002"]["Auth_Number"].startswith("AUTH")

    lines = dx.billing_lines(record)
    rows, _, _ = build_estimated_hospital_bill([{"CPT/HCPCS Code": "99214"}] + [{"CPT/HCPCS Code": line["code"]} for line in lines], "OPD")
    dx_codes = rcm_service.diagnosis_codes(record)
    claim_lines = rcm_service.claim_lines([row for row in rows if row["Pricing Status"] == "Priced"], lines, dx_codes)
    benefits = rcm_service.encounter_benefits(record, chart, rcm_service.benefit_rows(api))
    assert benefits["deductible_met"] == 80  # $80 patient liability from the paid March claim
    expected = rcm_service.adjudicate_lines(claim_lines, benefits, dx_codes)
    assert expected["status"] == "Paid" and expected["totals"]["copay"] == 25
    record["bill_estimate"] = {"claim_lines": claim_lines}

    account = rcm_service.save_send_to_rcm(record, chart, benefits, expected, collected_at_visit=25, client=api)
    assert account["Aging_Bucket"] == "Not yet billed" and account["Patient_Paid_At_Discharge"] == 25
    encounter_row = next(row for row in api.get_sheet("Encounters")["records"] if row["Encounter_ID"] == "ENC700100001")
    assert encounter_row["Primary_Diagnosis_Code"] == "E11.65" and encounter_row["Status"] == "Coded - sent to billing"

    claims = [{"Claim ID": "CLM-P", "CPT/HCPCS Codes": ["99214"], "ICD-10 Codes": dx_codes,
               "Patient Information": {"Member ID": "MBR1", "Payer": "Aetna"}},
              {"Claim ID": "CLM-I", "CPT/HCPCS Codes": ["83036", "70551", "36415"], "ICD-10 Codes": dx_codes,
               "Patient Information": {"Member ID": "MBR1", "Payer": "Aetna"}}]
    account, claims = rcm_service.submit_claims(account, claims, client=api)
    assert account["Claim_Status"] == "Accepted"
    with pytest.raises(ValueError, match="corrected claim"):  # re-sending must not wipe what the payer has
        rcm_service.save_send_to_rcm(record, chart, benefits, expected, client=api)
    posted, claims, adjudication = rcm_service.post_remittance(record, chart, account, claims, client=api, today=date(2026, 11, 25))
    assert posted["Claim_Status"] == "Paid" and posted["Payer_Paid"] == adjudication["totals"]["payer"]
    assert posted["Statement_Date"] == "2026-11-27" and claims[1]["Remittance"]["Paid Amount"] > 0
    benefits_row = next(row for row in api.get_sheet("Payer_Plan_Benefits")["records"] if row["Encounter_ID"] == "ENC700100001")
    assert benefits_row["Deductible_Met_After"] == 500  # the MRI used the rest of the deductible
    claim_lines_saved = [row for row in api.get_sheet("Billing_Claims")["records"] if row.get("Source") == "NuuCare RCM"]
    assert {row["CPT_HCPCS"] for row in claim_lines_saved} == {"99214", "83036", "70551", "36415"}

    # the next encounter sees the moved accumulators: no deductible left
    next_benefits = rcm_service.encounter_benefits({**record, "id": "ENC700100002", "ehr_encounter_id": "ENC700100002"},
                                                   chart, rcm_service.benefit_rows(api))
    assert next_benefits["remaining_deductible"] == 0

    paid = rcm.apply_payment(posted, posted["Delta_Patient_Balance"], today=date(2026, 12, 1))
    rcm_service.save_account(paid, api)
    accounts = rcm_service.load_accounts(True, api, today=date(2026, 12, 1))
    mine = next(row for row in accounts if row["Encounter_ID"] == "ENC700100001")
    assert mine["Delta_Patient_Balance"] == 0 and mine["Recommended_Action"] == "Close — no patient balance"
    assert any(row["Source"] == "EHR claim history" for row in accounts)
