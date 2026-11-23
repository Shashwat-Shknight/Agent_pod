"""Synthetic clinical scenarios for seeding (no real patients, no LLM needed).

Each scenario is one primary-care visit. The conversation carries Subjective, Objective and Assessment;
the Plan is left to clinical decision support, exactly like the live demo. Recommendations cite Harrison's
topics the same way the real CDS agent does. Chart rows (problems, medications, allergies, labs, vitals)
make the EHR side of each synthetic patient consistent with the visit.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    encounter_type: str
    transcript: str
    subjective: str
    objective: str
    assessment: str
    highlights: tuple[str, ...]
    recommendations: tuple[tuple[str, str], ...]
    reasoning: str
    safety: str
    passages: tuple[tuple[str, str], ...]
    problems: tuple[tuple[str, str], ...]
    medications: tuple[tuple[str, str, str, str], ...]  # name, dose, frequency, indication ICD-10
    labs: tuple[tuple[str, str, object, str, str, str], ...] = ()  # LOINC, test, result, unit, range, flag
    vitals: tuple[int, int, int, int, float] = (128, 80, 74, 98, 98.2)  # sys, dia, HR, SpO2, temp F
    allergy: tuple[str, str, str] | None = None  # substance, reaction, severity
    age_range: tuple[int, int] = (40, 80)
    sex: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def soap(self) -> str:
        return (f"Subjective:\n{self.subjective}\n\nObjective:\n{self.objective}\n\nAssessment:\n{self.assessment}\n\n"
                "Plan:\nPending review of clinical decision support before the treatment plan is finalised.")

    def recommendation_text(self) -> str:
        items = "\n".join(f"{i}. **{title}**: {body}" for i, (title, body) in enumerate(self.recommendations, 1))
        return f"**Recommendation**\n{items}\n\n**Reasoning**\n{self.reasoning}\n\n**Safety Note**\n{self.safety}"

    def guideline_text(self) -> str:
        return "\n\n".join(
            f"Source: Harrison's Principles of Internal Medicine, passage {100 + i * 7} · Topic: {topic}\n{text}"
            for i, (topic, text) in enumerate(self.passages, 1)
        )


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="diabetes_neuropathy",
        title="Uncontrolled type 2 diabetes with neuropathy, hypertension and hyperlipidemia",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: Good morning. What brings you in today?\n"
            "Patient: My sugars have been running high, mostly over 250, since I ran out of metformin a month ago.\n"
            "Patient: I am thirsty all the time and I have burning and tingling in both feet, worse at night.\n"
            "Doctor: Are you still taking lisinopril for your blood pressure?\n"
            "Patient: Yes, every day. I am allergic to penicillin, it gives me hives.\n"
            "Doctor: Your blood pressure today is 152 over 88. On the monofilament exam you have reduced sensation in both feet.\n"
            "Doctor: We collected blood today for an A1c, a lipid panel and a basic metabolic panel.\n"
            "Doctor: My assessment: type 2 diabetes, uncontrolled with high sugars. Diabetic peripheral neuropathy. "
            "Hypertension, not at goal. Hyperlipidemia, high cholesterol on the last panel.\n"
            "Doctor: Let me review the clinical decision support for each problem before I finalise your plan."
        ),
        subjective="Home glucose readings above 250 mg/dL since running out of metformin one month ago. Polydipsia. "
                   "Bilateral burning and tingling in both feet, worse at night. Takes lisinopril daily. Penicillin allergy (hives).",
        objective="BP 152/88. Monofilament exam: reduced sensation in both feet. Blood collected today for A1c, lipid panel "
                  "and basic metabolic panel.",
        assessment="1. Type 2 diabetes mellitus, uncontrolled with hyperglycemia.\n2. Diabetic peripheral neuropathy.\n"
                   "3. Essential hypertension, not at goal.\n4. Hyperlipidemia.",
        highlights=("Ran out of metformin", "Glucose > 250", "Burning feet", "BP 152/88", "Penicillin allergy"),
        recommendations=(
            ("Restart metformin and titrate", "Restart metformin 500 mg twice daily with meals, titrate to 1000 mg twice daily over 4 weeks if tolerated; recheck A1c in 3 months."),
            ("Add a thiazide to lisinopril", "Add chlorthalidone 12.5 mg daily; continue lisinopril. Target BP below 130/80."),
            ("Start a moderate-intensity statin", "Atorvastatin 20 mg nightly for diabetes with hyperlipidemia; repeat lipid panel in 6–12 weeks."),
            ("Neuropathy care", "Tight glycaemic control, annual foot exam, consider duloxetine if pain limits sleep."),
        ),
        reasoning="Hyperglycaemia follows metformin interruption; neuropathy and uncontrolled BP increase cardiovascular risk.",
        safety="No penicillins recommended. Check eGFR before metformin titration and potassium after starting a thiazide.",
        passages=(("Diabetes mellitus", "Metformin is first-line therapy for type 2 diabetes unless contraindicated by renal impairment."),
                  ("Hypertension", "Thiazide-type diuretics combined with ACE inhibitors lower blood pressure effectively."),
                  ("Lipoprotein disorders", "Statins reduce cardiovascular events in adults with diabetes aged 40–75.")),
        problems=(("E11.65", "Type 2 diabetes mellitus with hyperglycemia"), ("E11.42", "Type 2 diabetes with diabetic polyneuropathy"),
                  ("I10", "Essential hypertension"), ("E78.5", "Hyperlipidemia, unspecified")),
        medications=(("Lisinopril 20 mg tablet", "20 mg", "Daily", "I10"), ("Metformin 500 mg tablet", "500 mg", "Twice daily", "E11.65")),
        labs=(("4548-4", "Hemoglobin A1c", 9.4, "%", "4.0-5.6", "High"), ("2093-3", "Total cholesterol", 238, "mg/dL", "<200", "High"),
              ("2345-7", "Glucose", 262, "mg/dL", "70-99", "High")),
        vitals=(152, 88, 82, 97, 98.4),
        allergy=("Penicillin", "Hives", "Moderate"),
        age_range=(50, 75),
    ),
    Scenario(
        key="hypertension_followup",
        title="Hypertension follow-up, not at goal",
        encounter_type="Follow-up",
        transcript=(
            "Doctor: How has your blood pressure been at home?\n"
            "Patient: Mostly around 150 over 95. I get a dull headache in the mornings sometimes.\n"
            "Patient: I take amlodipine every day and I cut down on salt. I have no chest pain.\n"
            "Doctor: Blood pressure today is 158 over 96, heart rate 76. Heart and lungs sound normal.\n"
            "Doctor: We collected blood today for a basic metabolic panel to check kidney function and potassium.\n"
            "Doctor: Assessment: essential hypertension, not controlled on a single agent. Morning headache likely related.\n"
            "Doctor: I will look at the decision support before we settle the plan."
        ),
        subjective="Home BP around 150/95. Occasional dull morning headache. Adherent to amlodipine, reduced salt intake. Denies chest pain.",
        objective="BP 158/96, HR 76. Cardiac and lung exam normal. Blood collected today for basic metabolic panel.",
        assessment="1. Essential hypertension, uncontrolled on monotherapy.\n2. Morning headache, likely BP related.",
        highlights=("Home BP 150/95", "Morning headache", "On amlodipine"),
        recommendations=(
            ("Add an ACE inhibitor", "Add lisinopril 10 mg daily to amlodipine; recheck BP in 4 weeks."),
            ("Home BP monitoring", "Twice-daily home readings for 2 weeks; bring the log to the next visit."),
            ("Lifestyle", "DASH diet, sodium under 2 g/day, 150 minutes of moderate exercise weekly."),
        ),
        reasoning="BP remains above target on calcium-channel blocker monotherapy; combination therapy is recommended.",
        safety="Check potassium and creatinine 1–2 weeks after starting an ACE inhibitor. Avoid in pregnancy.",
        passages=(("Hypertension", "Most patients require two or more agents; ACE inhibitor plus calcium channel blocker is effective."),),
        problems=(("I10", "Essential hypertension"),),
        medications=(("Amlodipine 10 mg tablet", "10 mg", "Daily", "I10"),),
        labs=(("2160-0", "Creatinine", 1.0, "mg/dL", "0.6-1.3", "Normal"), ("2823-3", "Potassium", 4.3, "mmol/L", "3.5-5.1", "Normal")),
        vitals=(158, 96, 76, 98, 98.1),
        age_range=(45, 80),
    ),
    Scenario(
        key="asthma_exacerbation",
        title="Asthma exacerbation after a viral illness",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: What is going on with your breathing?\n"
            "Patient: My asthma has been bad for five days since a cold. I am wheezing and have a cough at night.\n"
            "Patient: I have used my albuterol inhaler four or five times a day. I do not smoke.\n"
            "Doctor: Oxygen saturation is 95 percent, respiratory rate 22. I hear expiratory wheezes in both lungs.\n"
            "Doctor: We did a chest x-ray today, two views, and it shows no pneumonia.\n"
            "Doctor: Assessment: moderate persistent asthma with acute exacerbation, triggered by a viral infection.\n"
            "Doctor: I want to check the decision support before we finalise your plan."
        ),
        subjective="Five days of wheeze and nocturnal cough after an upper respiratory infection. Using albuterol 4–5 times daily. Non-smoker.",
        objective="SpO2 95%, RR 22. Bilateral expiratory wheezes. Chest x-ray, 2 views, performed today: no consolidation.",
        assessment="1. Moderate persistent asthma with acute exacerbation.\n2. Recent viral upper respiratory infection.",
        highlights=("Wheeze 5 days", "Albuterol 4–5x/day", "CXR clear"),
        recommendations=(
            ("Short oral corticosteroid course", "Prednisone 40 mg daily for 5 days."),
            ("Step up controller therapy", "Start budesonide-formoterol as maintenance and reliever."),
            ("Asthma action plan", "Written action plan; return if SpO2 falls below 92% or speech is limited."),
        ),
        reasoning="Increased reliever use and wheeze indicate exacerbation; a short steroid burst shortens recovery.",
        safety="Monitor glucose if diabetic while on prednisone. No beta-blockers.",
        passages=(("Asthma", "Exacerbations are treated with short courses of systemic corticosteroids and inhaled bronchodilators."),),
        problems=(("J45.41", "Moderate persistent asthma with (acute) exacerbation"),),
        medications=(("Albuterol HFA 90 mcg inhaler", "2 puffs", "Every 4-6 hours as needed", "J45.41"),),
        vitals=(124, 78, 96, 95, 98.9),
        age_range=(18, 60),
    ),
    Scenario(
        key="pneumonia",
        title="Community-acquired pneumonia",
        encounter_type="Outpatient",
        transcript=(
            "Doctor: Tell me about your symptoms.\n"
            "Patient: I have had a fever up to 102 and a productive cough with yellow sputum for four days. I feel fatigue all the time.\n"
            "Patient: I have some pain on the right side when I breathe deeply. I am allergic to sulfa drugs.\n"
            "Doctor: Temperature 101.4, oxygen 94 percent. Crackles at the right lung base.\n"
            "Doctor: We performed a chest x-ray, two views, showing a right lower lobe infiltrate, and drew a complete blood count today.\n"
            "Doctor: Assessment: community-acquired pneumonia, right lower lobe, stable for outpatient care.\n"
            "Doctor: Let me check the decision support for antibiotic choice before we finalise the plan."
        ),
        subjective="Four days of fever to 102 F, productive cough with yellow sputum, fatigue, right-sided pleuritic pain. Sulfa allergy.",
        objective="T 101.4 F, SpO2 94%. Right basal crackles. Chest x-ray 2 views today: right lower lobe infiltrate. CBC drawn today.",
        assessment="1. Community-acquired pneumonia, right lower lobe (CURB-65 score 0).\n2. Fever and fatigue secondary to infection.",
        highlights=("Fever 102", "Productive cough", "RLL infiltrate", "Sulfa allergy"),
        recommendations=(
            ("Outpatient antibiotic", "Amoxicillin 1 g three times daily for 5 days; add doxycycline for atypical cover."),
            ("Return precautions", "Return if breathless, confused or SpO2 below 92%."),
            ("Follow-up", "Clinical review in 48–72 hours; repeat chest x-ray in 6 weeks if over 50 or a smoker."),
        ),
        reasoning="CURB-65 of 0 supports outpatient treatment with a beta-lactam plus macrolide or doxycycline.",
        safety="Sulfa allergy: avoid trimethoprim-sulfamethoxazole. Confirm no penicillin allergy before amoxicillin.",
        passages=(("Pneumonia", "Outpatients without comorbidities may be treated with amoxicillin or doxycycline."),),
        problems=(("J18.9", "Pneumonia, unspecified organism"),),
        medications=(),
        labs=(("6690-2", "WBC", 14.2, "10*3/uL", "4.0-11.0", "High"),),
        vitals=(118, 72, 104, 94, 101.4),
        allergy=("Sulfonamide antibiotics", "Rash", "Mild"),
        age_range=(25, 70),
    ),
    Scenario(
        key="depression_anxiety",
        title="Depression and anxiety follow-up",
        encounter_type="Telehealth",
        transcript=(
            "Doctor: How have your mood and sleep been since we last spoke?\n"
            "Patient: Still low most days. My depression makes it hard to get going, and the anxiety is worse at work.\n"
            "Patient: I sleep about five hours. I have no thoughts of harming myself.\n"
            "Doctor: Your PHQ-9 score today is 16 and GAD-7 is 13. You appear tired but your speech and thinking are clear.\n"
            "Doctor: Assessment: major depressive disorder, recurrent, moderate. Generalised anxiety disorder.\n"
            "Doctor: I will review the decision support before we agree the plan."
        ),
        subjective="Persistent low mood, poor motivation, work-related anxiety, sleeping 5 hours. Denies suicidal ideation.",
        objective="Telehealth visit. PHQ-9 16, GAD-7 13. Appears tired; speech and thought process normal.",
        assessment="1. Major depressive disorder, recurrent, moderate.\n2. Generalized anxiety disorder.",
        highlights=("PHQ-9 16", "GAD-7 13", "No suicidal ideation"),
        recommendations=(
            ("Start an SSRI", "Sertraline 50 mg daily, increase to 100 mg after 2–4 weeks if tolerated."),
            ("Psychotherapy referral", "Refer for cognitive behavioural therapy."),
            ("Safety and follow-up", "Crisis line information; follow up in 2 weeks, repeat PHQ-9 at 6 weeks."),
        ),
        reasoning="Moderate depression with anxiety responds best to an SSRI combined with psychotherapy.",
        safety="Warn about early activation; avoid combining with other serotonergic drugs (e.g. tramadol).",
        passages=(("Depression", "SSRIs are first-line pharmacotherapy; combine with psychotherapy for moderate depression."),),
        problems=(("F33.1", "Major depressive disorder, recurrent, moderate"), ("F41.1", "Generalized anxiety disorder")),
        medications=(),
        vitals=(122, 76, 80, 99, 98.0),
        age_range=(22, 65),
    ),
    Scenario(
        key="knee_osteoarthritis",
        title="Knee osteoarthritis with corticosteroid injection",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: How is your right knee?\n"
            "Patient: The arthritis pain is worse, especially on stairs, and it is stiff for 20 minutes each morning.\n"
            "Patient: Ibuprofen helps a little. I have obesity and I am trying to lose weight.\n"
            "Doctor: The right knee has crepitus and mild effusion, with reduced flexion. Your BMI is 33.\n"
            "Doctor: After consent we performed a corticosteroid injection into the right knee today under sterile technique.\n"
            "Doctor: Assessment: primary osteoarthritis of the right knee. Obesity.\n"
            "Doctor: Let me check the decision support before we finalise the plan."
        ),
        subjective="Worsening right knee pain on stairs, 20 minutes of morning stiffness, partial relief from ibuprofen. Trying to lose weight.",
        objective="Right knee crepitus, mild effusion, reduced flexion. BMI 33. Corticosteroid injection performed into right knee today.",
        assessment="1. Primary osteoarthritis, right knee.\n2. Obesity, BMI 33.",
        highlights=("Knee pain on stairs", "BMI 33", "Injection performed"),
        recommendations=(
            ("Structured exercise", "Refer to physical therapy for quadriceps strengthening."),
            ("Weight loss", "Target 5–10% weight loss; dietitian referral."),
            ("Topical NSAID", "Diclofenac gel to the knee four times daily instead of regular oral ibuprofen."),
        ),
        reasoning="Exercise and weight loss are core treatments; topical NSAIDs carry less systemic risk.",
        safety="Limit corticosteroid injections to every 3 months. Check renal function if oral NSAIDs continue.",
        passages=(("Osteoarthritis", "Exercise, weight loss and topical NSAIDs are first-line; intra-articular steroids give short-term relief."),),
        problems=(("M17.11", "Unilateral primary osteoarthritis, right knee"), ("E66.9", "Obesity, unspecified")),
        medications=(("Ibuprofen 400 mg tablet", "400 mg", "As needed", "M17.11"),),
        vitals=(134, 82, 72, 98, 98.3),
        age_range=(50, 85),
    ),
    Scenario(
        key="chest_pain",
        title="Atypical chest pain evaluation",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: You mentioned chest discomfort. Tell me more.\n"
            "Patient: I have had chest pain on and off for a week, sharp, on the left side, worse when I press on it.\n"
            "Patient: It is not related to exercise. I have no shortness of breath. I smoked for ten years but quit.\n"
            "Doctor: Blood pressure 130 over 82, heart rate 70. The pain is reproducible on palpation of the chest wall.\n"
            "Doctor: We drew a complete blood count today.\n"
            "Doctor: Assessment: chest pain, most consistent with costochondritis. Former smoker.\n"
            "Doctor: I will review the decision support before finalising the plan."
        ),
        subjective="One week of intermittent sharp left-sided chest pain, worse with pressure, not exertional. Denies dyspnoea. Former smoker.",
        objective="BP 130/82, HR 70. Pain reproducible on chest wall palpation. CBC drawn today.",
        assessment="1. Chest pain, likely musculoskeletal (costochondritis).\n2. Former smoking history.",
        highlights=("Reproducible chest pain", "Not exertional", "Former smoker"),
        recommendations=(
            ("Resting ECG", "Obtain a 12-lead ECG to exclude ischaemia given smoking history."),
            ("Analgesia", "Naproxen 250 mg twice daily with food for 7 days."),
            ("Red flags", "Seek urgent care for exertional pain, sweating or breathlessness."),
        ),
        reasoning="Reproducible, non-exertional pain suggests a chest wall cause; an ECG is low cost and reassuring.",
        safety="Avoid NSAIDs if history of GI bleeding or renal impairment.",
        passages=(("Chest discomfort", "Pain reproduced by palpation favours a musculoskeletal cause, but ischaemia must be considered."),),
        problems=(("R07.89", "Other chest pain"), ("Z87.891", "Personal history of nicotine dependence")),
        medications=(),
        labs=(("718-7", "Hemoglobin", 14.1, "g/dL", "13.0-17.0", "Normal"),),
        vitals=(130, 82, 70, 99, 98.2),
        age_range=(35, 65),
    ),
    Scenario(
        key="copd_smoking",
        title="COPD with chronic cough in a current smoker",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: How is your breathing and the cough?\n"
            "Patient: I have a cough every morning with phlegm and I get winded on one flight of stairs.\n"
            "Patient: I still smoke about a pack a day. I want to quit this time.\n"
            "Doctor: Oxygen saturation 93 percent. Prolonged expiration with scattered wheeze.\n"
            "Doctor: Spirometry today shows an FEV1 to FVC ratio of 0.62, FEV1 58 percent predicted.\n"
            "Doctor: Assessment: chronic obstructive pulmonary disease, moderate. Tobacco use, current smoker, ready to quit.\n"
            "Doctor: Let me review the decision support before the plan."
        ),
        subjective="Daily productive morning cough, dyspnoea on one flight of stairs. Smokes one pack per day, motivated to quit.",
        objective="SpO2 93%. Prolonged expiration, scattered wheeze. Spirometry today: FEV1/FVC 0.62, FEV1 58% predicted.",
        assessment="1. COPD, moderate (GOLD 2).\n2. Nicotine dependence, cigarettes, current smoker.",
        highlights=("Pack a day", "FEV1 58%", "Wants to quit"),
        recommendations=(
            ("Long-acting bronchodilator", "Start tiotropium once daily; albuterol as needed."),
            ("Smoking cessation", "Varenicline with counselling; set a quit date within 2 weeks."),
            ("Vaccination and rehab", "Pneumococcal and influenza vaccines; refer to pulmonary rehabilitation."),
        ),
        reasoning="Symptomatic GOLD 2 COPD benefits from a LAMA; quitting smoking is the only intervention that slows decline.",
        safety="Warn about neuropsychiatric effects of varenicline; reduce dose if renal impairment.",
        passages=(("COPD", "Smoking cessation and long-acting bronchodilators are the foundation of COPD management."),),
        problems=(("J44.9", "Chronic obstructive pulmonary disease, unspecified"), ("F17.210", "Nicotine dependence, cigarettes, uncomplicated")),
        medications=(("Albuterol HFA 90 mcg inhaler", "2 puffs", "As needed", "J44.9"),),
        vitals=(138, 84, 88, 93, 98.0),
        age_range=(50, 80),
    ),
    Scenario(
        key="obesity_prediabetes",
        title="Obesity with elevated glucose",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: You wanted to talk about your weight.\n"
            "Patient: I have gained 30 pounds in two years. I am tired and I snack a lot when I am stressed.\n"
            "Patient: My mother has diabetes and I worry about it.\n"
            "Doctor: Your weight is 112 kilograms, BMI 37. Blood pressure 136 over 86.\n"
            "Doctor: We checked a blood glucose today, fasting 118, and collected an A1c.\n"
            "Doctor: Assessment: obesity, class 2. Impaired fasting glucose, at risk of diabetes.\n"
            "Doctor: I will check the decision support before we make a plan."
        ),
        subjective="30 lb weight gain over two years, stress eating, tiredness. Family history of diabetes in mother.",
        objective="Weight 112 kg, BMI 37, BP 136/86. Blood glucose today 118 mg/dL fasting. A1c collected today.",
        assessment="1. Obesity, class 2 (BMI 37).\n2. Impaired fasting glucose.",
        highlights=("BMI 37", "Fasting glucose 118", "Family history"),
        recommendations=(
            ("Intensive lifestyle programme", "Diabetes prevention programme: 7% weight loss, 150 minutes of activity weekly."),
            ("Consider pharmacotherapy", "Discuss a GLP-1 receptor agonist if BMI stays above 30 after 3 months."),
            ("Recheck glycaemia", "Repeat A1c and fasting glucose in 6 months."),
        ),
        reasoning="Lifestyle intervention reduces progression to diabetes by about 58% in people with impaired fasting glucose.",
        safety="GLP-1 agonists are contraindicated with a personal or family history of medullary thyroid cancer.",
        passages=(("Obesity", "Lifestyle intervention is first-line; pharmacotherapy is considered for BMI ≥30 or ≥27 with comorbidity."),),
        problems=(("E66.01", "Morbid (severe) obesity due to excess calories"), ("R73.01", "Impaired fasting glucose")),
        medications=(),
        labs=(("1558-6", "Fasting glucose", 118, "mg/dL", "70-99", "High"),),
        vitals=(136, 86, 78, 98, 98.4),
        age_range=(28, 60),
    ),
    Scenario(
        key="migraine",
        title="Migraine with nausea",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: Tell me about the headaches.\n"
            "Patient: I get a throbbing headache on one side about six times a month, with nausea and light sensitivity.\n"
            "Patient: They last most of the day. Over-the-counter pain relievers barely help.\n"
            "Doctor: Your neurological exam is normal, blood pressure 118 over 74.\n"
            "Doctor: Assessment: migraine without aura, episodic, with nausea. No red flags, so no MRI is needed now.\n"
            "Doctor: Let me look at the decision support before we finalise the plan."
        ),
        subjective="Unilateral throbbing headache about 6 days per month with nausea and photophobia, lasting most of the day; poor response to OTC analgesics.",
        objective="Normal neurological examination. BP 118/74.",
        assessment="1. Migraine without aura, episodic, with nausea.",
        highlights=("6 headache days/month", "Nausea", "Normal neuro exam"),
        recommendations=(
            ("Acute treatment", "Sumatriptan 50 mg at onset, may repeat after 2 hours; metoclopramide for nausea."),
            ("Preventive therapy", "Consider propranolol or amitriptyline given 6 headache days per month."),
            ("Headache diary", "Track triggers and medication days; limit acute medication to under 10 days per month."),
        ),
        reasoning="Frequent migraines with poor OTC response warrant a triptan and discussion of prevention.",
        safety="Triptans are contraindicated in coronary or cerebrovascular disease and uncontrolled hypertension.",
        passages=(("Migraine", "Triptans are effective acute therapy; prophylaxis is considered with frequent attacks."),),
        problems=(("G43.009", "Migraine without aura, not intractable"),),
        medications=(),
        vitals=(118, 74, 68, 99, 98.1),
        age_range=(18, 50),
        sex="Female",
    ),
    Scenario(
        key="hyperlipidemia_statin",
        title="Hyperlipidemia on statin therapy",
        encounter_type="Follow-up",
        transcript=(
            "Doctor: How are you getting on with the atorvastatin?\n"
            "Patient: Fine, no muscle aches. I have been walking every day.\n"
            "Doctor: Blood pressure 126 over 80. We drew a lipid panel today.\n"
            "Doctor: Your previous LDL cholesterol was 162.\n"
            "Doctor: Assessment: hyperlipidemia on statin therapy, tolerating well.\n"
            "Doctor: I will check the decision support before finalising the plan."
        ),
        subjective="Taking atorvastatin without myalgia. Walking daily.",
        objective="BP 126/80. Lipid panel drawn today. Previous LDL 162 mg/dL.",
        assessment="1. Hyperlipidemia, on statin, tolerating well.",
        highlights=("No myalgia", "Prior LDL 162"),
        recommendations=(
            ("Continue statin", "Continue atorvastatin 20 mg; intensify to 40 mg if LDL reduction is under 30–49%."),
            ("Cardiovascular risk review", "Calculate 10-year ASCVD risk with today's lipid panel."),
        ),
        reasoning="Moderate-intensity statin should lower LDL by 30–49%; adjust to response.",
        safety="Check ALT only if symptoms of liver injury; review interactions (e.g. clarithromycin).",
        passages=(("Lipoprotein disorders", "Statin intensity is guided by baseline risk and LDL response."),),
        problems=(("E78.5", "Hyperlipidemia, unspecified"),),
        medications=(("Atorvastatin 20 mg tablet", "20 mg", "Nightly", "E78.5"),),
        labs=(("13457-7", "LDL cholesterol", 162, "mg/dL", "<100", "High"),),
        vitals=(126, 80, 70, 98, 98.0),
        age_range=(45, 80),
    ),
    Scenario(
        key="fatigue_anemia",
        title="Fatigue with suspected iron deficiency",
        encounter_type="Office Visit",
        transcript=(
            "Doctor: What has been bothering you?\n"
            "Patient: I have had fatigue for two months. I get short of breath on stairs and my periods are heavy.\n"
            "Patient: I do not have a fever. I eat little red meat.\n"
            "Doctor: You look pale. Heart rate 96, blood pressure 112 over 70.\n"
            "Doctor: We drew a complete blood count and iron studies today.\n"
            "Doctor: Assessment: fatigue, likely iron deficiency anemia from heavy menstrual bleeding.\n"
            "Doctor: Let me review the decision support before we decide the plan."
        ),
        subjective="Two months of fatigue, exertional breathlessness, heavy menstrual periods, low red meat intake. Denies fever.",
        objective="Pallor. HR 96, BP 112/70. CBC and iron studies drawn today.",
        assessment="1. Fatigue, likely iron deficiency anemia.\n2. Heavy menstrual bleeding.",
        highlights=("Fatigue 2 months", "Heavy periods", "Pallor"),
        recommendations=(
            ("Oral iron", "Ferrous sulfate 325 mg every other day; recheck hemoglobin in 4 weeks."),
            ("Address the bleeding", "Gynaecology review for heavy menstrual bleeding."),
            ("Diet", "Iron-rich foods with vitamin C to improve absorption."),
        ),
        reasoning="Heavy menstrual loss is the commonest cause of iron deficiency in premenopausal women.",
        safety="Separate iron from levothyroxine and antacids by 4 hours.",
        passages=(("Iron deficiency anemia", "Alternate-day oral iron improves absorption and tolerability."),),
        problems=(("D50.0", "Iron deficiency anemia secondary to blood loss (chronic)"), ("N92.0", "Excessive and frequent menstruation")),
        medications=(),
        labs=(("718-7", "Hemoglobin", 9.8, "g/dL", "12.0-15.5", "Low"), ("2276-4", "Ferritin", 6, "ng/mL", "15-150", "Low")),
        vitals=(112, 70, 96, 99, 98.3),
        age_range=(20, 48),
        sex="Female",
    ),
)

FIRST_NAMES = {
    "Female": ("Ava", "Maria", "Aisha", "Grace", "Sofia", "Mei", "Hannah", "Priya", "Chloe", "Fatima", "Lucia", "Naomi",
               "Elena", "Zara", "Ruth", "Keiko", "Amara", "Isabel", "Leah", "Nadia"),
    "Male": ("Liam", "Carlos", "Omar", "Ethan", "Raj", "Daniel", "Kenji", "Marcus", "Samuel", "Andre", "Luis", "Tariq",
             "Noah", "Victor", "Hiro", "Isaac", "Mateo", "Kwame", "Jonah", "Arjun"),
}
LAST_NAMES = ("Alvarez", "Brooks", "Chen", "Dubois", "Evans", "Fischer", "Garcia", "Hughes", "Ibrahim", "Jensen", "Kowalski",
              "Lopez", "Mensah", "Nakamura", "Okafor", "Patel", "Quinn", "Rossi", "Silva", "Thompson", "Usman", "Varga",
              "Walsh", "Xu", "Yilmaz", "Zimmerman", "Bennett", "Castillo", "Delgado", "Foster")
CITIES = (("Seattle", "WA", "98101"), ("Austin", "TX", "73301"), ("Denver", "CO", "80202"), ("Columbus", "OH", "43004"),
          ("Raleigh", "NC", "27601"), ("Phoenix", "AZ", "85001"), ("Portland", "OR", "97201"), ("Atlanta", "GA", "30301"),
          ("Madison", "WI", "53703"), ("Boston", "MA", "02108"))
STREETS = ("Oak Avenue", "Maple Street", "Cedar Lane", "Pine Road", "Elm Drive", "Lakeview Court", "Hillcrest Way", "River Road")
PAYERS = (("Athena Health Insurance", "PPO"), ("Northstar Medicare Advantage", "Medicare Advantage"),
          ("Summit Commercial", "HMO"), ("State Medicaid Plan", "Medicaid"))
