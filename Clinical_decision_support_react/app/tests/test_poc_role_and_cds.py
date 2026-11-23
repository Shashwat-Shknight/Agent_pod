"""POC rules: clinicians never reach the coding screen; documented specificity drives ICD-10; CDS is per-problem."""

from app.agent.cds_agent import encounter_problems, grounded_guideline_context, problem_query
from app.agent.context import EncounterContext
from app.domain import access_control as ac
from app.domain.admin_service import CLINICIAN_ROLES, DEFAULT_PERMISSIONS
from app.domain.encounter_pipeline import (
    DIABETES_WITH_HYPERGLYCEMIA,
    DIABETES_WITH_NEUROPATHY,
    coding_term,
    get_detected_insights,
    is_documented_cpt_candidate,
)


def _user(role):
    return {"username": "u1", "role": role, "active": True}


def test_clinicians_cannot_open_coding_but_coders_can():
    store = ac._with_defaults({"users": [], "role_permissions": {}, "audit": []})
    for role in CLINICIAN_ROLES:
        assert not DEFAULT_PERMISSIONS[role]["Codes"]["View"]
        assert not ac.can_view_page(store, _user(role), "codes")
        assert ac.can_view_page(store, _user(role), "review")
    assert ac.can_view_page(store, _user("Coding Specialist"), "codes")
    assert ac.can_view_page(store, _user("Super Admin"), "codes")


def test_stored_permissions_cannot_grant_coding_to_clinicians():
    """A matrix saved before this rule (e.g. in Snowflake) is corrected on load and on save."""
    legacy = {"Clinical Reviewer": {"Codes": {"View": True, "Edit": True, "Approve": False, "Admin": False}}}
    store = ac._with_defaults({"users": [], "role_permissions": legacy, "audit": []})
    assert store["role_permissions"]["Clinical Reviewer"]["Codes"]["View"] is False
    ac.save_role_permissions(store, "Physician / APP", [{"Module": "Codes", "View": True, "Edit": True}], "admin")
    assert store["role_permissions"]["Physician / APP"]["Codes"]["View"] is False
    assert not ac.can(store, _user("Physician / APP"), "Codes", "View")


def test_documented_diabetes_complications_replace_generic_diabetes():
    text = "type 2 diabetes, uncontrolled with high sugars. diabetic peripheral neuropathy in both feet. hypertension."
    insights = get_detected_insights(text)
    assert DIABETES_WITH_NEUROPATHY in insights and DIABETES_WITH_HYPERGLYCEMIA in insights
    assert "Diabetes (Type 2)" not in insights  # E11.9 is never coded alongside a specific E11 code
    assert get_detected_insights("my diabetes is fine") == ["Diabetes (Type 2)"]


def test_hyperlipidemia_uses_official_wording_and_labs_are_detected():
    insights = get_detected_insights("hyperlipidemia. we collected blood for a lipid panel and basic metabolic panel")
    assert {"Hyperlipidemia", "Lipid Panel", "Basic Metabolic Panel"} <= set(insights)
    assert coding_term("Hyperlipidemia") == "Hyperlipidemia, unspecified"


def test_office_visit_is_documented_by_the_encounter():
    assert is_documented_cpt_candidate("Established Patient Office Visit", "Doctor: How are you today?")
    assert not is_documented_cpt_candidate("Established Patient Office Visit", "")


def test_cds_problems_come_from_the_assessment():
    numbered = EncounterContext(assessment="1) Type 2 diabetes, uncontrolled. 2) Diabetic neuropathy. 3) Hypertension not at goal.")
    assert encounter_problems(numbered) == ["Type 2 diabetes, uncontrolled", "Diabetic neuropathy", "Hypertension not at goal"]
    sentence = EncounterContext(patient_summary="Assessment: Hypertension, hyperlipidemia and asthma\n\nPlan:\nFollow up")
    assert encounter_problems(sentence) == ["Hypertension", "hyperlipidemia", "asthma"]
    assert "statin" in problem_query("Hyperlipidemia")
    assert "monofilament" in problem_query("Diabetic neuropathy")


def test_documented_allergy_is_not_flagged_but_a_cross_reactive_order_is():
    from app.domain.ehr_chart import medication_safety_checks

    chart = {
        "patient": {"Patient_ID": "P1"},
        "records": {"Allergies": [{"Substance": "Penicillin", "Category": "Drug", "Clinical_Status": "Active"}]},
    }
    documented = medication_safety_checks(chart, "Allergic to penicillin (hives). Avoid penicillin-class antibiotics.")
    ordered = medication_safety_checks(chart, "Allergic to penicillin. Start amoxicillin 500 mg three times daily.")
    assert documented[0][1] == "Pass"
    assert ordered[0][1] == "Attention" and "amoxicillin" in ordered[0][3]


def test_validation_failures_and_unpriced_codes_travel_to_rcm_as_exceptions():
    from app.domain.billing import _is_em_visit, rcm_exceptions

    checks = [
        {"name": "Claim scrub · imaging mismatch", "status": "fail", "detail": "CT head billed for chest CT"},
        {"name": "Diagnosis specificity", "status": "warn", "detail": "E78.5 unspecified"},
    ]
    exceptions = rcm_exceptions(checks, ["93000"])
    assert [item["check"] for item in exceptions] == ["Claim scrub · imaging mismatch", "Unpriced service"]
    assert rcm_exceptions([], []) == []
    assert _is_em_visit("99213", "") and _is_em_visit("", "Established Patient Office Visit")
    assert not _is_em_visit("83036", "A1C Test")


def test_cds_evidence_is_retrieved_per_problem(monkeypatch):
    import app.rag.retriever as retriever

    def fake_chunks(query, results=2):
        return [{"text": f"passage about {query}", "metadata": {"source": "knowledge_base_harrison.docx", "chunk": len(query)}}]

    monkeypatch.setattr(retriever, "retrieve_chunks", fake_chunks)
    context = grounded_guideline_context(EncounterContext(assessment="1) Hypertension 2) Gout"))
    assert context.count("Source: Harrison's Principles of Internal Medicine") == 2
    assert "Topic: Hypertension" in context and "Topic: Gout" in context
