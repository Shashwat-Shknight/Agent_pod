import os
import uuid
from datetime import date

import pytest

from app.reference import lookup, sources
from frontend.claim_validator import validate_claim
from frontend.coding_validation import code_set_checks, post_coding_checks

ORDER_SAMPLE = "\n".join([
    "00001 A00     0 Cholera                                                      Cholera",
    "00002 A000    1 Cholera due to Vibrio cholerae 01, biovar cholerae           Cholera due to Vibrio cholerae 01, biovar cholerae",
    "11613 I1A0    1 Resistant hypertension                                       Resistant hypertension",
    "garbage line",
])


def test_parse_cdc_order_file():
    rows = sources.parse_icd10cm_order_file(ORDER_SAMPLE)
    assert [(row.code, row.code_raw, row.is_billable) for row in rows] == [
        ("A00", "A00", False), ("A00.0", "A000", True), ("I1A.0", "I1A0", True)]
    assert rows[1].long_description == "Cholera due to Vibrio cholerae 01, biovar cholerae"


def test_release_checksum_is_order_independent():
    rows = sources.parse_icd10cm_order_file(ORDER_SAMPLE)
    a = sources.Release("ICD10CM", "FYX", date(2026, 10, 1), "t", "u", rows)
    b = sources.Release("ICD10CM", "FYX", date(2026, 10, 1), "t", "u", list(reversed(rows)))
    assert a.checksum == b.checksum


@pytest.mark.parametrize("header, children, expected", [
    ("R51", [("R510", "R51.0", "Headache with orthostatic component, not elsewhere classified"),
             ("R519", "R51.9", "Headache, unspecified")], "R51.9"),
    ("J45", [("J45901", "J45.901", "Unspecified asthma with (acute) exacerbation"),
             ("J45909", "J45.909", "Unspecified asthma, uncomplicated"),
             ("J4520", "J45.20", "Mild intermittent asthma, uncomplicated")], "J45.909"),
    ("M545", [("M5450", "M54.50", "Low back pain, unspecified"), ("M5459", "M54.59", "Other low back pain")], "M54.50"),
])
def test_header_resolves_to_default_billable_child(header, children, expected):
    assert lookup.default_billable_child(header, children)[0] == expected


def test_default_child_none_without_billable_descendants():
    assert lookup.default_billable_child("Z99", []) is None


@pytest.mark.parametrize("code, hint, expected", [
    ("I10", None, "ICD10CM"), ("R51.9", None, "ICD10CM"), ("J0013", None, "HCPCS"), ("99213", None, "CPT"),
    ("A0101", "ICD", "ICD10CM"), ("A0101", "CPT", "HCPCS"), ("0001F", "CPT", "CPT"), ("hello", None, ""),
])
def test_classify(code, hint, expected):
    assert lookup.classify(code, hint) == expected


def test_as_date_accepts_app_formats():
    assert lookup.as_date("19/11/2026") == date(2026, 11, 19)
    assert lookup.as_date("2026-11-19T10:00:00") == date(2026, 11, 19)


def test_code_set_check_fails_with_replacements_and_notes_cpt():
    results = [
        {"code": "R51", "system": "ICD10CM", "ok": False, "status": "not_billable", "message": "Category header",
         "replacements": [{"code": "R51.0"}, {"code": "R51.9"}]},
        {"code": "99213", "system": "CPT", "ok": True, "status": "unverified", "message": "AMA"},
    ]
    [check] = code_set_checks(results)
    assert check["status"] == "fail" and "R51.0, R51.9" in check["detail"]
    [ok] = code_set_checks([results[1], {"code": "I10", "system": "ICD10CM", "ok": True}])
    assert ok["status"] == "pass" and "1 CPT" in ok["detail"]
    assert code_set_checks(None) == []


def test_post_coding_checks_include_code_set_result():
    rows = [{"key": "ICD:R51", "type": "ICD-10-CM", "code": "R51", "status": "Accepted", "description": "Headache"}]
    checks = post_coding_checks(rows, code_set_results=[{"code": "R51", "system": "ICD10CM", "ok": False,
                                                         "message": "not billable"}])
    assert any(check["name"] == "Official code set" and check["status"] == "fail" for check in checks)


def test_claim_validator_uses_code_set_validator():
    def validator(codes, dos, hint):
        return [{"code": code, "system": "CPT" if hint == "CPT" else "ICD10CM",
                 "ok": hint == "CPT" or code != "R51", "message": "checked",
                 "replacements": [{"code": "R51.9"}] if code == "R51" else []} for code in codes]

    claim = {"Claim ID": "C1", "ICD-10 Codes": ["R51", "I10"], "CPT/HCPCS Codes": ["99213"], "Charges": []}
    report = validate_claim(claim, code_set_validator=validator)
    code_checks = [check for check in report["Checks"] if check["Category"] == "Code Set Validation"]
    assert {check["Status"] for check in code_checks} == {"ERROR", "PASS"}
    assert any("Use R51.9" in error for error in report["Errors"])


# ---------------------------------------------------------------------------
# Live Snowflake: type-2 history behaviour of apply_release (SNOWFLAKE_INTEGRATION=1)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.environ.get("SNOWFLAKE_INTEGRATION") != "1", reason="set SNOWFLAKE_INTEGRATION=1")
def test_apply_release_history(monkeypatch):
    from app.db import client, settings
    from app.reference import sync

    monkeypatch.setenv("STORAGE_BACKEND", "snowflake")
    settings.reset_cache()
    system = f"IT{uuid.uuid4().hex[:6].upper()}"

    def row(code, text, billable=True):
        return sources.CodeRow(code, code, text, text, billable)

    try:
        first = sources.Release(system, "R1", date(2025, 10, 1), "test", "", [row("X1", "One"), row("X2", "Two"), row("X3", "Three")])
        second = sources.Release(system, "R2", date(2026, 10, 1), "test", "", [row("X1", "One"), row("X2", "Two revised"), row("X4", "Four")])
        assert sync.apply_release(first, "itest")["added"] == 3
        assert sync.apply_release(second, "itest") == {"codes": 3, "added": 1, "changed": 1, "retired": 1}
        versions = {(r["CODE_RAW"], str(r["VALID_FROM"]), str(r["VALID_TO"])) for r in client.query(
            "SELECT CODE_RAW, VALID_FROM, VALID_TO FROM REFERENCE.CODES WHERE CODE_SYSTEM = %s", (system,))}
        assert versions == {("X1", "2025-10-01", "None"), ("X2", "2025-10-01", "2026-09-30"), ("X2", "2026-10-01", "None"),
                            ("X3", "2025-10-01", "2026-09-30"), ("X4", "2026-10-01", "None")}
        third = sources.Release(system, "R3", date(2027, 10, 1), "test", "", [row("X1", "One"), row("X2", "Two revised"), row("X3", "Three"), row("X4", "Four")])
        assert sync.apply_release(third, "itest")["added"] == 0  # X3 re-added -> new version, not "new code"
        assert client.query("SELECT COUNT(*) N FROM REFERENCE.CODES WHERE CODE_SYSTEM = %s AND CODE_RAW = 'X3'", (system,))[0]["N"] == 2
    finally:
        client.execute("DELETE FROM REFERENCE.CODES WHERE CODE_SYSTEM = %s", (system,))
        client.execute("DELETE FROM REFERENCE.CODE_SET_RELEASES WHERE CODE_SYSTEM = %s", (system,))
