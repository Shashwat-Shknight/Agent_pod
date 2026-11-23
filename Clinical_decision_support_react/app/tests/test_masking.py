"""Role-based PHI masking: formats, per-role policy, API responses, write guards, and the Snowflake mirror."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from app.domain import masking as m
from app.domain.admin_service import DEFAULT_PERMISSIONS
from app.tests.test_web_api import H, PASSWORD, _consultation, _login, _setup_team, _user, api  # noqa: F401

PATIENT = {"patient_id": "P100001", "name": "Olivia Johnson", "mrn": "MRN500001", "dob": "1940-01-01T00:00:00",
           "mobile": "(555) 202-1002", "email": "olivia.johnson@example.test", "street_address": "102 Main Street",
           "city": "Seattle", "state": "WA", "zip": "98101"}


# ---------------------------------------------------------------- formats
def test_mask_formats():
    assert m.mask_name("Olivia Johnson") == "O*** J***"
    assert m.mask_dob("1962-03-04", today=date(2026, 11, 23)) == "1962-**-**"
    assert m.mask_dob("23/11/1930", today=date(2026, 11, 23)) == "1936 or earlier"  # 90+ aggregated (Safe Harbor)
    assert m.mask_identifier("MRN500031") == "*****0031"
    assert m.mask_identifier("12") == "****"
    assert m.mask_contact("mobile", "(555) 202-1002") == "(***) ***-1002"
    assert m.mask_contact("email", "olivia.johnson@example.test") == "o***@***.test"
    assert m.mask_contact("zip", "98101") == "981**"
    assert m.mask_contact("streetaddress", "102 Main Street") == "***"
    assert m.redact_text("Call 555-202-1002 or a@b.com, SSN 123-45-6789") == "Call (***) ***-**** or ***@***, SSN ***-**-****"


def test_policy_masks_only_hidden_classes_and_never_changes_the_input():
    original = dict(PATIENT)
    contact_only = m.Policy({m.CONTACT}).apply(PATIENT)
    assert contact_only["name"] == "Olivia Johnson" and contact_only["mrn"] == "MRN500001"
    assert contact_only["mobile"] == "(***) ***-1002" and contact_only["city"] == "***" and contact_only["state"] == "WA"
    everything = m.Policy({m.IDENTITY, m.CONTACT, m.NARRATIVE}).apply({"record": {**PATIENT, "transcript": "Hi", "transcript_data": [{"Speaker": "Patient", "Text": "Hi"}]}})
    rec = everything["record"]
    assert rec["name"] == "O*** J***" and rec["mrn"] == "*****0001" and rec["dob"] == "1940-**-**"
    assert rec["transcript"] == m.NARRATIVE_MASK and rec["transcript_data"] == []
    assert rec["patient_id"] == "P100001"  # internal surrogate key stays usable
    assert PATIENT == original


def test_generic_name_is_masked_only_when_the_object_is_about_a_patient():
    policy = m.Policy({m.IDENTITY})
    assert policy.apply({"name": "Dr. Elena Ruiz", "role": "Clinical Reviewer"})["name"] == "Dr. Elena Ruiz"
    assert policy.apply({"name": "Ava Chen", "mrn": "MRN1"})["name"] == "A*** C***"
    assert policy.apply({"First_Name": "Ava", "Legal_Name": "Ava Chen"}) == {"First_Name": "A***", "Legal_Name": "A*** C***"}
    assert policy.apply({"patient": "Ava Chen", "lines": []})["patient"] == "A*** C***"
    assert policy.apply({"patient": {"Legal_Name": "Ava Chen"}})["patient"]["Legal_Name"] == "A*** C***"


def test_objects_under_text_keys_are_walked_not_erased():
    out = m.Policy({m.NARRATIVE, m.IDENTITY}).apply({"summary": {"id": "PT-1", "name": "Ava Chen", "mrn": "MRN123456"}, "transcript": "x"})
    assert out["summary"] == {"id": "PT-1", "name": "A*** C***", "mrn": "*****3456"} and out["transcript"] == m.NARRATIVE_MASK


def test_contact_masking_scrubs_phone_and_email_inside_readable_notes():
    out = m.Policy({m.CONTACT}).apply({"summary": "Reach her at 555-202-1002.", "note": {"text": "email a@b.com"}})
    assert out == {"summary": "Reach her at (***) ***-****.", "note": {"text": "email ***@***"}}


def test_default_role_policy_is_minimum_necessary():
    def unmasked(role):
        return {phi for phi, module in m.MODULES.items() if DEFAULT_PERMISSIONS[role][module]["View"]}

    assert unmasked("Clinical Reviewer") == {m.IDENTITY, m.CONTACT, m.NARRATIVE}
    assert unmasked("Coding Specialist") == {m.IDENTITY, m.NARRATIVE}
    assert unmasked("RCM Analyst") == {m.IDENTITY, m.CONTACT}
    assert unmasked("Operations Admin") == {m.IDENTITY}
    assert unmasked("Executive") == set() and unmasked("Auditor") == set()
    assert unmasked("Super Admin") == {m.IDENTITY, m.CONTACT, m.NARRATIVE}


def test_snowflake_function_uses_the_same_classification():
    migrations = sorted((Path(__file__).resolve().parents[2] / "snowflake" / "migrations").glob("V*.sql"))
    sql = [p.read_text(encoding="utf-8") for p in migrations if "FUNCTION GOVERNANCE.REDACT_PHI" in p.read_text(encoding="utf-8")][-1]
    assert "NARRATIVE_KEYS.has(k) && isObject" in sql  # objects under text keys are walked, as in Python

    def js_set(name):
        found = re.search(rf"const {name} = new Set\(\[([^\]]*)\]\)", sql)
        assert found, name
        return set(re.findall(r'"([a-z0-9]+)"', found.group(1)))

    assert js_set("NAME_KEYS") == m.NAME_KEYS
    assert js_set("CONTEXT_NAME_KEYS") == m.CONTEXT_NAME_KEYS
    assert js_set("DOB_KEYS") == m.DOB_KEYS
    assert js_set("ID_KEYS") == m.ID_KEYS
    assert js_set("CONTACT_KEYS") == m.CONTACT_KEYS
    assert js_set("NARRATIVE_KEYS") == m.NARRATIVE_KEYS
    assert js_set("PATIENT_MARKERS") == m.PATIENT_MARKERS


# ---------------------------------------------------------------- through the API
def _as(api, username, role):
    _login(api, "admin", PASSWORD)
    _user(api, username, role)
    _login(api, username)
    _login(api, username, PASSWORD + "9")


def test_coder_sees_contact_masked_and_doctor_sees_everything(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)
    record_id, _ = _consultation(api)
    full = api.get("/api/patients/P100001")
    assert "X-NuuCare-Masked" not in full.headers and "@" in full.json()["patient"]["email"]

    _login(api, "coder.lee", password)
    detail = api.get("/api/patients/P100001")
    assert detail.headers["X-NuuCare-Masked"] == "contact"
    patient = detail.json()["patient"]
    assert patient["mobile"].startswith("(***) ***-") and patient["email"].startswith(patient["email"][0] + "***@")
    assert patient["name"] == full.json()["patient"]["name"]  # identity is visible to coders
    assert detail.json()["permissions"]["masked"] == ["contact"]
    etag = patient["etag"]
    refused = api.put("/api/patients/P100001", headers=H, json={"etag": etag, "mobile": "(555) 000-0000"})
    assert refused.status_code in (403,) and refused.json()["error"]["code"] in ("phi_masked", "forbidden")


def test_identity_masked_role_cannot_search_by_name_or_read_narrative(api):
    password = _setup_team(api)
    _login(api, "dr.carter", password)
    record_id, _ = _consultation(api)
    _as(api, "exec.kim", "Executive")
    listing = api.get("/api/consultations").json()["consultations"]
    row = next(r for r in listing if r["id"] == record_id)
    assert "***" in row["name"] and row["mrn"].startswith("****")
    assert api.get("/api/patients?q=olivia").json()["total"] == 0  # no name oracle
    assert api.get("/api/patients?q=P100001").json()["total"] == 1
    view = api.get(f"/api/consultations/{record_id}").json()
    assert view["record"]["transcript"] == m.NARRATIVE_MASK
    assert view["permissions"] == {"edit": False, "sign": False}
    assert api.get("/api/me").json()["name"] == "Exec.Kim"  # own profile is never masked


def test_administration_endpoints_are_not_masked(api):
    password = _setup_team(api)
    _login(api, "admin", PASSWORD)
    users = api.get("/api/admin/users")
    assert "X-NuuCare-Masked" not in users.headers


def test_unmasking_is_a_normal_permission(api):
    password = _setup_team(api)
    _login(api, "admin", PASSWORD)
    matrix = api.get("/api/admin/roles").json()
    assert {"PHI: Identity", "PHI: Contact details", "PHI: Clinical narrative"} <= set(matrix["modules"])
    rows = [{"Module": "PHI: Contact details", "View": True, "Edit": False, "Approve": False, "Admin": False}]
    assert api.put("/api/admin/roles/Coding Specialist", headers=H, json={"rows": rows}).status_code == 200
    _login(api, "coder.lee", password)
    assert "X-NuuCare-Masked" not in api.get("/api/patients/P100001").headers
