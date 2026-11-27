"""Negated or family-history mentions must not become diagnoses; chronic 'history of' stays active."""

from frontend.encounter_pipeline import get_detected_insights


def test_negated_mentions_are_not_coded():
    assert get_detected_insights("Patient denies chest pain. Has hypertension.") == ["Hypertension"]
    assert get_detected_insights("No fever or cough. Headache since Monday.") == ["Headache"]
    assert get_detected_insights("negative for asthma") == []


def test_history_of_a_chronic_condition_is_still_active():
    assert get_detected_insights("History of hypertension, on lisinopril.") == ["Hypertension"]


def test_family_history_is_not_the_patient():
    assert get_detected_insights("Family history of diabetes. Mother has asthma.") == []
    assert get_detected_insights("Mother has asthma and I have asthma too") == ["Asthma"]


def test_one_affirmed_mention_is_enough():
    assert "Chest Pain" in get_detected_insights("No chest pain yesterday, but today chest pain started.")


def test_negation_does_not_cross_a_clause_break():
    assert get_detected_insights("No nausea. Diabetes is poorly controlled.") == ["Type 2 diabetes mellitus with hyperglycemia"]


def test_risk_and_screening_language_is_not_a_diagnosis():
    assert get_detected_insights("Impaired fasting glucose, at risk of diabetes.") == []
    assert get_detected_insights("Screening for depression today. Exercise to prevent obesity.") == []
    assert get_detected_insights("At risk for diabetes, but I have hypertension") == ["Hypertension"]


def test_a_question_is_not_a_finding():
    transcript = "Doctor: Any chest pain or shortness of breath?\nPatient: No chest pain. Some tingling in both feet."
    assert "Chest Pain" not in get_detected_insights(transcript)
    assert "Fever" in get_detected_insights("Doctor: Do you have a fever?\nPatient: Yes, a fever since Monday.")
