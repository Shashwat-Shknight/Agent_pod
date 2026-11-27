"""Policy is data: packs load with metadata, can be overridden per site, and drive every agent."""

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.rules import packs
from frontend import diagnostics as dx
from frontend import hcc, prior_auth as pa, rcm

PACK_NAMES = ("rcm_policy", "claim_edits", "prior_auth", "hcc_v28", "diagnostic_catalog", "clinical_lexicon", "care_gaps", "providers")
PPO = {"Payer_Name": "UnitedHealthcare Demo PPO", "Plan_Type": "PPO", "Member_ID": "M1", "Status": "Active",
       "Annual_Deductible_USD": 1500, "Copay_USD": 20}


def test_every_pack_loads_with_metadata():
    for name in PACK_NAMES:
        pack = packs.load_pack(name)
        assert pack["pack"] == name and pack["version"] and pack["effective"] and pack["owner"]
    assert {row["pack"] for row in packs.describe()} >= set(PACK_NAMES)
    assert all(row["version"] != "INVALID" for row in packs.describe())


def test_a_site_can_override_a_pack_without_code(tmp_path, monkeypatch):
    policy = json.loads(packs.pack_path("rcm_policy").read_text(encoding="utf-8"))
    policy["statement_policy"]["terms_days"] = 45
    (tmp_path / "rcm_policy.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("RULES_DIR", str(tmp_path))
    account = rcm.refresh_account({"Patient_Responsibility": 100, "Claim_Status": "Paid", "Statement_Date": "2026-11-01"},
                                  today=date(2026, 11, 2))
    assert account["Payment_Due_Date"] == "2026-12-16"
    monkeypatch.delenv("RULES_DIR")
    assert rcm.statement_policy()["terms_days"] == 30


def test_invalid_pack_is_refused(tmp_path, monkeypatch):
    (tmp_path / "care_gaps.json").write_text(json.dumps({"pack": "care_gaps"}), encoding="utf-8")
    monkeypatch.setenv("RULES_DIR", str(tmp_path))
    with pytest.raises(packs.RulePackError):
        packs.load_pack("care_gaps")


def test_federal_plan_year_limits_drive_benefits():
    medicare = rcm.plan_benefits({**PPO, "Payer_Name": "Medicare Demo"}, date(2026, 3, 1))
    assert medicare["annual_deductible"] == 283 and medicare["copay"] == 0 and medicare["oop_max"] is None
    assert rcm.plan_benefits({**PPO, "Payer_Name": "Medicare Demo"}, date(2025, 3, 1))["annual_deductible"] == 257
    later = packs.plan_year_values("rcm_policy", 2031)
    assert later["fallback"] and later["plan_year"] == 2026
    ma = rcm.plan_benefits({**PPO, "Payer_Name": "Cigna Demo", "Plan_Type": "Medicare"}, date(2026, 3, 1))
    assert ma["payer_class"] == "Medicare Advantage" and ma["oop_max"] <= 9250
    assert rcm.plan_benefits(PPO, date(2026, 3, 1))["oop_max"] <= 10600


def test_contract_rates_are_learned_from_the_hospital_chargemaster():
    athena = rcm.contract_terms({"Payer_Name": "Athena Health Insurance", "Plan_Type": "PPO"})
    assert "Hospital chargemaster" in athena["label"] and 0.4 < athena["allowed_rate"] < 0.8 and athena["by_cpt"]
    unknown = rcm.contract_terms({"Payer_Name": "Aetna Demo Choice POS", "Plan_Type": "POS"})
    assert unknown["allowed_rate"] == 0.60 and "synthetic" in unknown["label"]


def test_per_cpt_contract_rate_is_used_in_adjudication():
    benefits = rcm.plan_benefits({"Payer_Name": "Athena Health Insurance", "Plan_Type": "PPO", "Status": "Active",
                                  "Copay_USD": 0, "Annual_Deductible_USD": 0}, date(2026, 3, 1))
    code, rate = next(iter(benefits["allowed_rates_by_cpt"].items()))
    line = rcm.adjudicate([rcm.ClaimLine(code, "x", 100.0, ("Z00.00",))], benefits)["lines"][0]
    assert line["allowed"] == round(100 * rate, 2)


def test_cdm_prior_auth_flag_is_honoured():
    need = pa.requirement("97110", {"Payer_Name": "Aetna Demo", "Plan_Type": "POS"})
    assert need["required"] and "Hospital CDM" in need["reason"]
    assert not pa.requirement("70551", {"Payer_Name": "Medicare Demo"})["required"]


def test_hcc_seed_mapping_and_hierarchies():
    assert hcc.hcc_for("E11.42") == "HCC 37" and hcc.hcc_for("E1165") == "HCC 38" and hcc.hcc_for("I10") == ""
    assert hcc.apply_hierarchy(["HCC 38", "HCC 37", "HCC 226", "HCC 224", "HCC 329", "HCC 326"]) == ["HCC 37", "HCC 224", "HCC 326"]
    assert hcc.label("HCC 155").startswith("Major Depression")


def test_official_cms_mapping_overrides_the_seed(tmp_path, monkeypatch):
    workbook = tmp_path / "2026_mappings.xlsx"
    rows = [["2026 Midyear Final ICD-10-CM Mappings", "", "", ""], ["", "", "", ""],
            ["Diagnosis Code", "Description", "CMS-HCC Model Category V24", "CMS-HCC Model Category V28"],
            ["E1142", "Type 2 diabetes mellitus with diabetic polyneuropathy", "18", "37"],
            ["I10", "Essential (primary) hypertension", "", ""],
            ["Z9981", "Dependence on supplemental oxygen", "", "213"]]
    pd.DataFrame(rows).to_excel(workbook, header=False, index=False)
    from app.scripts import import_hcc_mappings

    target = tmp_path / "hcc_v28_mappings.csv"
    monkeypatch.setattr(import_hcc_mappings, "OFFICIAL_MAPPING", target)
    monkeypatch.setattr(hcc, "OFFICIAL_MAPPING", target)
    assert import_hcc_mappings.main([str(workbook)]) == 0
    assert target.read_text(encoding="utf-8").splitlines() == ["code,hcc", "E1142,37", "Z9981,213"]
    assert hcc.hcc_for("Z99.81") == "HCC 213" and "official" in hcc.source()


def test_diagnostic_catalog_is_data(tmp_path, monkeypatch):
    catalog = json.loads(packs.pack_path("diagnostic_catalog").read_text(encoding="utf-8"))
    for test in catalog["tests"]:
        if test["key"] == "a1c":
            test["interval_days"] = 30
    (tmp_path / "diagnostic_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    chart = {"records": {"Labs": [{"LOINC_Code": "4548-4", "Result_Date": "2026-10-01", "Result": 7.9, "Unit": "%"}]}}
    assert not dx.due_status(dx.by_key()["a1c"], dx.last_results(chart), date(2026, 11, 28))["due"]
    monkeypatch.setenv("RULES_DIR", str(tmp_path))
    assert dx.due_status(dx.by_key()["a1c"], dx.last_results(chart), date(2026, 11, 28))["due"]
    assert len(dx.CATALOG) == len(catalog["tests"])  # module attribute follows the pack


def test_unknown_payer_gateway_is_explained(monkeypatch):
    from frontend import payer_gateway

    monkeypatch.setenv("PAYER_GATEWAY", "availity")
    with pytest.raises(RuntimeError, match="no connector"):
        payer_gateway.get_gateway()
    monkeypatch.setenv("PAYER_GATEWAY", "simulated")
    assert payer_gateway.get_gateway().name == "simulated"


def test_outpatient_facility_claim_uses_outpatient_type_of_bill():
    from frontend.claim_generation_page import default_bill_type, default_place_of_service, provider_npi

    assert default_bill_type("OPD") == "131" and default_bill_type("IPD") == "111" and default_place_of_service() == "22"
    assert provider_npi("Someone Unlisted") == packs.load_pack("providers")["default_npi"]


def test_llm_and_cds_settings_come_from_the_environment():
    import importlib

    from app.core import config

    os.environ["CDS_MAX_PROBLEMS"] = "3"
    try:
        assert importlib.reload(config).CDS_MAX_PROBLEMS == 3
    finally:
        os.environ.pop("CDS_MAX_PROBLEMS")
        importlib.reload(config)
    assert config.SCRIBE_TEMPERATURE == 0.2 and config.CDS_MAX_PROBLEMS == 5
