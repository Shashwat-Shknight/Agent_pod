"""Regression: a patient's "headache" must never turn a documented chest CT into a head CT (70450)."""

import pytest

from code_matcher import canonicalize_documented_procedure, ct_body_site
from app.domain.billing_scrubber import _expected_imaging, scrub_claim
from app.domain.coding import apply_coder_overrides

NOTE = (
    "Subjective: fever, slight headache and frequent sneezing.\n"
    "Plan: Order CT scan of the chest, chest X‑ray, and HbA1c test."
)


@pytest.mark.parametrize("text, site", [
    (NOTE, "chest"),
    ("I have a slight headache I suggest CT Scan chest X-ray hba1c test", "chest"),
    ("Plan: CT head without contrast", "head"),
    ("order a head CT", "head"),
    ("hit his head on the floor, CT scan ordered", "head"),
    ("headache, order CT scan", None),          # site unknown: left to the coder, never guessed as head
    ("CT of the thorax", "chest"),
    ("computed tomography of the brain", "head"),
    ("headache, no imaging", None),
])
def test_ct_body_site(text, site):
    assert ct_body_site(text) == site


def test_generic_ct_canonicalized_from_documented_site():
    assert canonicalize_documented_procedure("CT scan", NOTE) == "ct scan of chest"
    assert canonicalize_documented_procedure("CT scan", "headache. CT head without contrast") == "ct scan of head without contrast"


def test_scrubber_expects_chest_ct_not_head_ct():
    assert ("71250", "CT chest without contrast") in _expected_imaging(NOTE)
    assert not any(code in {"70450", "70460"} for code, _ in _expected_imaging(NOTE))


def _scrub(codes, note=NOTE):
    services = [{"CPT/HCPCS Code": code, "quantity": 1} for code in codes]
    return scrub_claim(note, services, priced_lines=[], valid_cpt_codes=codes)


def test_chest_ct_documented_with_article_is_supported():
    report = _scrub(["71250", "71045", "83036"])
    assert not [e for e in report["errors"] if e["type"] in {"unsupported_cpt", "imaging_mismatch"}]


def test_head_ct_billed_for_chest_ct_note_recommends_71250():
    report = _scrub(["70450"])
    mismatch = [e for e in report["errors"] if e["type"] == "imaging_mismatch"]
    assert mismatch and mismatch[0]["expected_code"] == "71250"
    assert any(r["from_code"] == "70450" and r["to_code"] == "71250" for r in report["recommended_cpt_changes"])


def test_coder_override_releases_only_documentation_support_edit():
    report = {"errors": [
        {"type": "unsupported_cpt", "code": "93000", "message": "not supported"},
        {"type": "imaging_mismatch", "code": "70450", "message": "wrong body part"},
    ], "warnings": []}
    overrides = {"93000": {"by": "coder1", "at": "19 Nov 2026"}, "70450": {"by": "coder1", "at": "19 Nov 2026"}}
    result = apply_coder_overrides(report, overrides)
    assert [e["type"] for e in result["errors"]] == ["imaging_mismatch"]          # wrong body part still blocks
    assert result["warnings"][0]["type"] == "coder_attested" and "coder1" in result["warnings"][0]["message"]
    assert apply_coder_overrides(report, {}) is report


def test_duplicate_codes_render_once():
    from app.domain.encounter_pipeline import dedupe_code_results
    from app.domain.coding import build_code_rows, encounter_code_results

    record = {"id": "PT-1", "summary": "Plan: CT scan of the chest.", "code_results": {
        "icd10": [{"ICD-10 Code": "R51.9", "Extracted Condition": "Headache"},
                  {"ICD-10 Code": "R51.9", "Extracted Condition": "Headache, unspecified"}],
        "cpt": [{"CPT/HCPCS Code": "71250", "Extracted Procedure": "CT scan of chest"},
                {"CPT/HCPCS Code": "71250", "Extracted Procedure": "ct chest"}]}}
    results = encounter_code_results(record)
    assert [item["CPT/HCPCS Code"] for item in results["cpt"]] == ["71250"]
    keys = [row["key"] for row in build_code_rows(record, record["code_results"])]
    assert len(keys) == len(set(keys)) == 2
    assert dedupe_code_results(None) is None
