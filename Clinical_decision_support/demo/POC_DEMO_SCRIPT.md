# POC demo script: Ambient-to-Closed-Loop Encounter Pod

One diabetic chronic-care visit, end to end. Every step below was run against the live app.
Use synthetic data only.

## Before the demo (5 minutes)

1. Start the app: backend `uvicorn app.main:app --port 8000`, frontend `streamlit run frontend/streamlit_app.py`.
2. In **User Access**, create two users (any passwords):
   - `dr.carter`, role **Clinical Reviewer** (the doctor)
   - `coder.lee`, role **Coding Specialist** (the coder)

   Super Admin sees every screen. Sign in as the doctor to show that the doctor never sees codes.
3. Keep this transcript ready to paste (or read it aloud with a colleague and record it).

## Act 1: The doctor (sign in as `dr.carter`)

The sidebar has no **Coding & HCC** entry. The doctor's role cannot open it.

1. **Consultations → New consultation**: choose existing EHR patient **P100031 (William Young)**.
2. **Record consultation**: open *No audio? Paste a transcript instead*, paste the transcript below,
   then click **Generate summary from transcript**.
   - The ambient scribe drafts the SOAP note and key highlights.
3. **Proceed to Clinical Review & CDS**. Talking points:
   - **Recommendations**: one per diagnosis, each citing *Harrison's Principles of Internal Medicine, passage N*:
     - restart and titrate metformin;
     - add a second antihypertensive to lisinopril;
     - start a statin;
     - risk-factor control for neuropathy.
   - **Evidence** tab: the Harrison's passages retrieved for each diagnosis (labelled *Topic: ...*).
   - **Medication Safety**: penicillin allergy check (pass), plus duplicate therapy and polypharmacy checks.
   - **Care Gaps · VBC**: blood pressure not at goal, diabetic eye exam open, kidney health evaluation due.
   - **Coding hand-off**: shows *Awaiting signature*. No codes and no link to the coding screen.
4. Accept or **Push to note** the recommendations you agree with.
5. **Proceed to SOAP Note → Sign & Finalize**. The note locks and the banner reads
   *Signed and locked · sent to the coding queue*. The doctor's work is done.

## Act 2: The coder (sign in as `coder.lee`)

1. **Coding & HCC**: open the signed encounter. Expected suggestions:

   | Type | Code | Description |
   |---|---|---|
   | ICD-10-CM | E11.42 | Type 2 diabetes mellitus with diabetic polyneuropathy |
   | ICD-10-CM | E11.65 | Type 2 diabetes mellitus with hyperglycemia |
   | ICD-10-CM | I10 | Essential (primary) hypertension |
   | ICD-10-CM | E78.5 | Hyperlipidemia, unspecified |
   | CPT | 99213 | Established patient office visit |
   | CPT | 83036 | Hemoglobin A1c |
   | CPT | 80061 | Lipid panel |
   | CPT | 80048 | Basic metabolic panel |

   Talking points:
   - The specific codes (E11.42 and E11.65, not E11.9) come from what the doctor said.
   - The codes are checked against the live CDC/CMS code sets.
   - E11.42 and E11.65 carry HCC weight.
2. **Billing & RCM**: the claim scrub has no errors. It raises one coder review: *99213 vs 99214*,
   because the visit covered several chronic conditions plus labs. The coder decides; the system never upcodes.
   All four CPT codes are priced from the chargemaster.

## The transcript (paste exactly)

```
Doctor: Good morning, William. What brings you in today?
Patient: Good morning, doctor. My diabetes has been bad lately. My sugar readings at home have been running high, mostly between 220 and 260 in the mornings.
Doctor: Are you still taking your metformin?
Patient: No, I ran out about two months ago and did not get it refilled.
Doctor: Any other symptoms?
Patient: I am thirsty all the time and I get up at night to pass urine. And for the last few months I have numbness, tingling and burning in both feet, worse at night.
Doctor: Any cuts or sores on your feet?
Patient: No, nothing like that.
Doctor: And your blood pressure medicine?
Patient: I take lisinopril 10 milligrams every morning. I have not missed it.
Doctor: Any allergies?
Patient: Yes, I am allergic to penicillin. It gives me hives.
Doctor: Your blood pressure today is 152 over 88. On your foot exam, sensation to the monofilament is decreased in both feet, the skin is intact, and the foot pulses are normal.
Patient: Is that serious?
Doctor: It is something we need to take care of. My assessment today: first, type 2 diabetes, uncontrolled with high sugars, because you have been off metformin. Second, diabetic peripheral neuropathy in both feet. Third, hypertension that is not at goal. Fourth, hyperlipidemia; your cholesterol has been high in the past and you are not on a statin.
Patient: What do we do now?
Doctor: We will restart metformin 500 milligrams twice daily with meals. Today we collected blood for an A1c, lipid panel and basic metabolic panel to check your kidney function. Check your feet every day and wear well-fitting shoes. I will review the decision support recommendations for your blood pressure and cholesterol before I finalise the plan. Please come back in four weeks.
Patient: Thank you, doctor.
```

## If you read it aloud instead of pasting

Some exact words make the codes come out right. Keep them:

| Say | Why |
|---|---|
| "uncontrolled with high sugars" | Codes E11.65 (diabetes with hyperglycemia) instead of E11.9 |
| "diabetic peripheral neuropathy" | Codes E11.42 |
| "hypertension", "hyperlipidemia" / "cholesterol" | Codes I10 and E78.5 |
| "Today we collected blood for an A1c, lipid panel and basic metabolic panel" | "collected" marks the labs as performed, so they are billed |

Avoid words that add unwanted codes: fatigue, headache, fever, cough, chest pain, x-ray, smoking, injection, blood glucose.

## Mapping to the BRD

| BRD | Where it shows |
|---|---|
| 5.1 Ambient Scribe | SOAP draft generated from the conversation |
| 5.2 CDS, grounded | Per-diagnosis Harrison's evidence with passage citations |
| 5.3 Coding & HCC, human gate | Codes only on the coder's screen; the coder accepts or overrides |
| 5.6 RCM | Claim scrub before drop; E/M level left to the coder |
| 5.7 VBC | Care gaps: BP control, eye exam, kidney health |
| 5.8 Governance | Draft until signed; clinicians never code; audit trail |
