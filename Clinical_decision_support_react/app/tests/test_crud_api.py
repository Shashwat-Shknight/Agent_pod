"""Create / read / update / delete for patients, consultations and users, with their guard rails."""

from __future__ import annotations

from app.services import ehr_json_service
from app.tests.test_web_api import H, PASSWORD, _bootstrap, _consultation, _login, _setup_team, _user, api  # noqa: F401
from app.web import records

NEW = {"first_name": "Test", "last_name": "Patient", "dob": "1980-05-17", "sex": "Female", "city": "Austin", "email": "t.p@example.test"}


def _register(client, **changes):
    return client.post("/api/patients", headers=H, json={**NEW, **changes})


# ---------------------------------------------------------------- patients
def test_patient_full_crud_with_duplicate_and_stale_checks(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)

    created = _register(api)
    assert created.status_code == 200, created.text
    patient_id = created.json()["patient_id"]

    duplicate = _register(api)  # same name + DOB
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "possible_duplicate"
    assert patient_id in duplicate.json()["error"]["fields"]["patient_ids"]
    assert _register(api, allow_duplicate=True).status_code == 200

    detail = api.get(f"/api/patients/{patient_id}").json()
    assert detail["patient"]["name"] == "Test Patient" and detail["patient"]["city"] == "Austin"
    assert detail["permissions"]["delete"] is True  # fresh registration, no chart history
    etag = detail["patient"]["etag"]

    updated = api.put(f"/api/patients/{patient_id}", headers=H, json={"etag": etag, "last_name": "Renamed", "mobile": "(555) 111-2222"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["patient"]["name"] == "Test Renamed" and updated.json()["patient"]["mobile"] == "(555) 111-2222"

    stale = api.put(f"/api/patients/{patient_id}", headers=H, json={"etag": etag, "city": "Denver"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_record"

    listing = api.get("/api/patients?q=renamed&with_counts=true").json()
    assert listing["total"] == 1 and listing["patients"][0]["consultations"] == 0

    new_etag = updated.json()["patient"]["etag"]
    assert api.delete(f"/api/patients/{patient_id}?etag={etag}", headers=H).status_code == 409  # stale etag
    assert api.delete(f"/api/patients/{patient_id}?etag={new_etag}", headers=H).status_code == 200
    assert api.get(f"/api/patients/{patient_id}").status_code == 404


def test_patient_validation_errors(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)
    assert _register(api, dob="2999-01-01").status_code == 422
    assert _register(api, email="not-an-email").status_code == 422
    assert _register(api, first_name="   ").status_code == 422
    patient_id = _register(api).json()["patient_id"]
    etag = api.get(f"/api/patients/{patient_id}").json()["patient"]["etag"]
    blank = api.put(f"/api/patients/{patient_id}", headers=H, json={"etag": etag, "first_name": "  "})
    assert blank.status_code == 422
    assert api.put("/api/patients/P999999", headers=H, json={"etag": etag}).status_code == 404


def test_patient_with_consultations_or_history_cannot_be_deleted_by_a_clinician(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)
    record_id, _ = _consultation(api)  # on P100001, which also has rich chart history
    detail = api.get("/api/patients/P100001").json()
    assert detail["permissions"]["delete"] is False and len(detail["consultations"]) >= 1
    in_use = api.delete(f"/api/patients/P100001?etag={detail['patient']['etag']}", headers=H)
    assert in_use.status_code == 409 and in_use.json()["error"]["code"] == "patient_in_use"

    other = api.get("/api/patients/P100002").json()  # chart history, no consultations
    refused = api.delete(f"/api/patients/P100002?etag={other['patient']['etag']}", headers=H)
    assert refused.status_code == 403 and "EHR Demo Data" in refused.json()["error"]["message"]


def test_renaming_a_patient_updates_their_consultation_headers(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)
    record_id, _ = _consultation(api)
    etag = api.get("/api/patients/P100001").json()["patient"]["etag"]
    assert api.put("/api/patients/P100001", headers=H, json={"etag": etag, "first_name": "Olive"}).status_code == 200
    assert records.get_record(record_id)["name"].startswith("Olive ")


# ---------------------------------------------------------------- consultations
def test_draft_consultation_delete_rules(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)
    record_id, _ = _consultation(api)
    version = api.get(f"/api/consultations/{record_id}").json()["summary"]["version"]
    assert api.delete(f"/api/consultations/{record_id}?version={version + 5}", headers=H).json()["error"]["code"] == "stale_record"

    # another clinician cannot delete someone else's draft
    _login(api, "admin", PASSWORD)
    _user(api, "dr.other", "Clinical Reviewer")
    _login(api, "dr.other")
    _login(api, "dr.other", PASSWORD + "9")
    assert api.delete(f"/api/consultations/{record_id}?version={version}", headers=H).status_code == 403

    _login(api, "dr.carter", password)
    deleted = api.delete(f"/api/consultations/{record_id}?version={version}", headers=H)
    assert deleted.status_code == 200, deleted.text
    assert api.get(f"/api/consultations/{record_id}").status_code == 404
    encounters = ehr_json_service.get_patient_chart("P100001")["records"]["Encounters"]
    assert any(e.get("Status") == "Cancelled" for e in encounters)  # kept in the EHR, marked cancelled


def test_signed_consultation_cannot_be_deleted(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)
    record_id, _ = _consultation(api)
    view = api.get(f"/api/consultations/{record_id}").json()
    signed = api.post(f"/api/consultations/{record_id}/sign", headers=H, json={
        "version": view["summary"]["version"], "summary": view["record"]["summary"].replace("Not documented.", "Metformin restarted.")})
    assert signed.status_code == 200, signed.text
    version = signed.json()["summary"]["version"]
    refused = api.delete(f"/api/consultations/{record_id}?version={version}", headers=H)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "note_signed"


# ---------------------------------------------------------------- users
def test_user_delete_rules_and_released_work(api):
    _setup_team(api)
    _login(api, "admin", PASSWORD)
    assert api.delete("/api/admin/users/admin", headers=H).json()["error"]["code"] == "self_lockout"
    assert api.delete("/api/admin/users/nobody", headers=H).status_code == 404

    # work assigned to dr.carter goes back to the unassigned queue when the account is deleted
    _login(api, "dr.carter", PASSWORD + "9")
    record_id, _ = _consultation(api)
    assert records.get_record(record_id)["assigned_to"] == "dr.carter"
    _login(api, "admin", PASSWORD)
    deleted = api.delete("/api/admin/users/dr.carter", headers=H)
    assert deleted.status_code == 200 and deleted.json()["released"] == 1
    assert "dr.carter" not in [u["username"] for u in deleted.json()["users"]]
    assert records.get_record(record_id)["assigned_to"] is None
    assert _login(api, "dr.carter", PASSWORD + "9").status_code == 401

    _login(api, "admin", PASSWORD)
    edited = api.patch("/api/admin/users/coder.lee", headers=H, json={"name": "Lee Coder", "email": "lee@example.test"})
    assert edited.status_code == 200
    assert next(u for u in edited.json()["users"] if u["username"] == "coder.lee")["email"] == "lee@example.test"


def test_last_super_admin_cannot_be_deleted(api):
    _bootstrap(api)
    _user(api, "admin2", "Super Admin")
    _login(api, "admin2")
    _login(api, "admin2", PASSWORD + "9")
    assert api.delete("/api/admin/users/admin", headers=H).status_code == 200  # another super admin remains
    from app.domain import access_control as ac

    store = {"users": [{"username": "solo", "role": ac.SUPER_ADMIN, "active": True}], "audit": []}
    try:
        ac.delete_user(store, "solo", "test")
        raise AssertionError("expected refusal")
    except ValueError as error:
        assert "Super Admin" in str(error)


def test_bulk_ehr_helpers_write_once(api, monkeypatch):
    writes = []
    original = ehr_json_service._write_store
    monkeypatch.setattr(ehr_json_service, "_write_store", lambda payload: (writes.append(1), original(payload)))
    ehr_json_service.add_records_bulk({
        "Patient_Master": [{"Patient_ID": "P299998", "First_Name": "Bulk", "Last_Name": "One"},
                           {"Patient_ID": "P299999", "First_Name": "Bulk", "Last_Name": "Two"}],
        "Problems": [{"Problem_ID": "X1", "Patient_ID": "P299998", "ICD_10_CM": "I10"}],
    })
    assert len(writes) == 1
    assert ehr_json_service.delete_patients(["P299998", "P299999"]) == 2
    assert len(writes) == 2
    assert not ehr_json_service.list_patient_records("P100001", "Problems") == []  # other patients untouched


def test_react_screen_paths_never_collide_with_api_paths():
    """A browser refresh on a screen must get the React page, not an API answer."""
    import re
    from pathlib import Path

    from app.main import API_PREFIXES

    app_tsx = (Path(__file__).resolve().parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    screens = {path.split("/")[1] for path in re.findall(r'<Route path="(/[^"]*)"', app_tsx) if path not in ("/", "*")}
    assert screens and not screens & API_PREFIXES, screens & API_PREFIXES
