import pytest
from fastapi.testclient import TestClient

import app.services.ehr_json_service as ehr
from app.main import app
from frontend import chargemaster


@pytest.fixture
def store(tmp_path, monkeypatch):
    payload = {
        "format": "nuucare-workbook-json-v1",
        "sheets": {
            "Patient_Master": {"headers": ["Patient_ID", "MRN", "First_Name", "Last_Name"], "records": [
                {"Patient_ID": "P100001", "MRN": "MRN500001", "First_Name": "Olivia", "Last_Name": "Johnson"},
                {"Patient_ID": "P100002", "MRN": "MRN500050", "First_Name": "Liam", "Last_Name": "Smith"},
            ]},
            "Encounters": {"headers": ["Encounter_ID", "Patient_ID", "Date"], "records": [
                {"Encounter_ID": "ENC0011", "Patient_ID": "P100001", "Date": "2026-09-20"},
            ]},
        },
    }
    path = tmp_path / "ehr.json"
    monkeypatch.setattr(ehr, "JSON_PATH", path)
    ehr._atomic_write(payload)
    return path


def test_new_patient_gets_next_mrn_in_the_facility_series(store):
    patient = ehr.create_patient({"First_Name": "Ava", "Last_Name": "Brown"})
    assert patient["Patient_ID"] == "P100003" and patient["MRN"] == "MRN500051"
    assert ehr.create_patient({"First_Name": "Noah", "Last_Name": "Lee"})["MRN"] == "MRN500052"


def test_mrn_series_starts_fresh_when_none_exist():
    assert ehr._new_mrn([]) == "MRN1000001"
    assert ehr._new_mrn([{"MRN": "MRN-ABC"}]) == "MRN1000001"


def test_encounters_get_sequential_hospital_ids_linked_to_the_patient(store):
    first = ehr.add_patient_record("P100001", "Encounters", {"Date": "2026-11-28", "MRN": "MRN500001"})
    second = ehr.add_patient_record("P100002", "Encounters", {"Date": "2026-11-28"})
    assert first["Encounter_ID"] == "ENC700100001" and second["Encounter_ID"] == "ENC700100002"
    assert first["Patient_ID"] == "P100001" and first["MRN"] == "MRN500001"
    assert "MRN" in ehr._read_store()["sheets"]["Encounters"]["headers"]


def test_new_sheets_are_created_and_get_identifiers(store):
    order = ehr.add_patient_record("P100001", "Orders_Procedures", {"Encounter_ID": "ENC700100001", "Status": "Ordered"})
    assert order["Order_ID"] == "ORD30000001"
    request = ehr.add_patient_record("P100001", "Prior_Authorizations", {"Order_ID": order["Order_ID"], "Status": "Required"})
    assert request["PA_ID"] == "PA50000001"
    with pytest.raises(ValueError):
        ehr.add_patient_record("P100001", "bad sheet!", {})


def test_upsert_inserts_updates_and_refuses_another_patients_key(store):
    saved = ehr.upsert_patient_records("P100001", "Orders_Procedures", [{"Status": "Ordered"}, {"Status": "Ordered"}], "Order_ID")
    assert [row["Order_ID"] for row in saved] == ["ORD30000001", "ORD30000002"]
    updated = ehr.upsert_patient_records("P100001", "Orders_Procedures", [{"Order_ID": "ORD30000001", "Status": "Resulted"}], "Order_ID")
    assert updated[0]["Status"] == "Resulted"
    rows = ehr.list_sheet("Orders_Procedures")["records"]
    assert len(rows) == 2 and rows[0]["Status"] == "Resulted"
    with pytest.raises(ValueError, match="another patient"):
        ehr.upsert_patient_records("P100002", "Orders_Procedures", [{"Order_ID": "ORD30000001", "Status": "x"}], "Order_ID")
    account = ehr.upsert_patient_records("P100001", "Patient_Balance", [{"Account_ID": "ENC700100001", "Charge": 100}], "Account_ID")
    assert account[0]["Account_ID"] == "ENC700100001"
    with pytest.raises(ValueError):
        ehr.upsert_patient_records("P100001", "Patient_Balance", [{"Charge": 1}], "Account_ID")


def test_upsert_and_sheet_routes(store):
    client = TestClient(app)
    response = client.post("/patients/P100001/records/Prior_Authorizations/upsert",
                           json={"key": "PA_ID", "records": [{"Status": "Submitted"}]})
    assert response.status_code == 200 and response.json()["records"][0]["PA_ID"] == "PA50000001"
    assert client.get("/ehr/sheets/Prior_Authorizations").json()["records"][0]["Status"] == "Submitted"
    assert client.get("/ehr/sheets/Nothing_Here").json()["records"] == []
    bad = client.post("/patients/P100001/records/Orders_Procedures/upsert", json={"key": "Order_ID", "records": "x"})
    assert bad.status_code == 400
    missing = client.post("/patients/P999/records/Orders_Procedures/upsert", json={"key": "Order_ID", "records": []})
    assert missing.status_code == 404


def test_supplemental_cdm_prices_codes_the_hospital_chargemaster_lacks():
    lines = {line["cpt_hcpcs"]: line for line in chargemaster.price_cpt_bill_lines(
        [{"CPT/HCPCS Code": code} for code in ("99203", "93000", "36415", "83036", "00000")], "OPD")}
    assert lines["99203"]["unit_charge_usd"] == 290.0 and lines["99203"]["rate_source"] == "Supplemental CDM"
    assert lines["93000"]["rate_source_status"] == "Found"
    assert lines["83036"]["rate_source"] == "Hospital chargemaster"  # the hospital's own price wins
    assert lines["00000"]["unit_charge_usd"] is None
