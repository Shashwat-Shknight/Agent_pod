# POC demo script: Ambient Scribe → CDS → Coding & HCC → Billing & RCM

One diabetic chronic-care visit, end to end. Use synthetic data only.

**The story in one line:** the conversation gives the **S, O and A** of the SOAP note.
The doctor does **not** say the plan. **CDS writes the Plan**, the doctor signs, then the coder
codes (ICD-10, CPT, HCC) and the bill goes to RCM.

| SOAP part | Where it comes from |
|---|---|
| **S**ubjective | Patient's words: high sugars, ran out of metformin, thirst, burning feet, meds, allergy |
| **O**bjective | Doctor's exam: BP 152/88, monofilament exam, labs collected today |
| **A**ssessment | Doctor says 4 problems out loud |
| **P**lan | **Not spoken.** Built from the CDS recommendations ("Push to note") |

## Before the demo (5 minutes)

1. Start the backend and frontend (see the setup steps).
2. In **User Access**, create two users:
   - `dr.carter`, role **Clinical Reviewer** (the doctor)
   - `coder.lee`, role **Coding Specialist** (the coder)
3. Keep the transcript below ready to paste, or read it aloud with a colleague.

## Act 1: The doctor (sign in as `dr.carter`)

The sidebar has no **Coding & HCC** entry: the doctor can never see codes.

1. **Consultations → New consultation**, then choose patient **P100031 (William Young)**.
2. **Record consultation**: open *No audio? Paste a transcript instead*, paste the transcript,
   then click **Generate summary from transcript**.
3. Show the SOAP draft:
   - Subjective, Objective and Assessment are filled in.
   - The Plan only says *"Review clinical decision support ... finalize treatment plan"*.
   - Say: *"The doctor hasn't decided the plan yet. That's what CDS is for."*
4. **Proceed to Clinical Review & CDS**. One recommendation per problem, each citing
   *Harrison's Principles of Internal Medicine, passage N*:
   1. Restart and titrate metformin (the patient ran out of it).
   2. Add a second blood-pressure drug, a thiazide, to lisinopril. CDS keeps the current ACE inhibitor.
   3. Start a moderate-intensity statin.
   4. Control sugar, BP and lipids to slow the neuropathy.
   - **Evidence** tab: the Harrison's passages for each problem (*Topic: ...*).
   - **Medication Safety**: the penicillin allergy check passes, because none of the drugs are penicillins.
   - **Care Gaps · VBC**: BP not at goal, eye exam open, kidney check due.
   - **Coding hand-off**: *Awaiting signature*. No codes, no link to coding.
5. Click **Push to note ↗** on each recommendation. Each one is added to the **Plan**.
   Say: *"The plan is now written by CDS and approved by the doctor."*
6. **Proceed to SOAP Note**. The Plan now holds the 4 CDS items. Click **Sign & Finalize**.
   The banner says *Signed and locked · sent to the coding queue*. The doctor's job is done.

## Act 2: The coder (sign in as `coder.lee`)

1. **Coding & HCC**: open the signed visit. Expected codes:

   | Type | Code | Description | HCC (V28) |
   |---|---|---|---|
   | ICD-10-CM | E11.42 | Type 2 diabetes with diabetic polyneuropathy | HCC 37 |
   | ICD-10-CM | E11.65 | Type 2 diabetes with hyperglycemia | HCC 38 |
   | ICD-10-CM | I10 | Essential (primary) hypertension | none |
   | ICD-10-CM | E78.5 | Hyperlipidemia, unspecified | none |
   | CPT | 99213 | Established patient office visit | |
   | CPT | 83036 | Hemoglobin A1c | |
   | CPT | 80061 | Lipid panel | |
   | CPT | 80048 | Basic metabolic panel | |

   Talking points:
   - **Diagnoses come from the Assessment** the doctor spoke. Specific codes (E11.42, E11.65), not
     the vague E11.9, because the doctor said *"uncontrolled with high sugars"* and *"neuropathy"*.
   - **HCC**: both diabetes codes carry risk weight. In the V28 hierarchy HCC 37 outranks HCC 38,
     so the risk score counts HCC 37.
   - **MEAT check**: the HCCs are supported because the note has Monitor/Evaluate (sugar readings,
     A1c, exam), Assess (the Assessment) and Treat (the CDS plan: metformin, statin).
     The plan from CDS is what completes the "T".
   - **CPT comes from what was done today**: labs collected and the visit itself. Drugs started in the
     plan are not billed here.
   - Codes are checked against the live CDC/CMS code sets. The coder accepts or overrides each one.
2. **Billing & RCM**: the claim scrub runs, all 4 CPT codes are priced from the chargemaster.
   - One coder review is raised: *99213 vs 99214* (4 chronic problems plus labs). The coder decides.
     The system never upcodes.
   - Tick the attestation and click **Send to RCM**. The bill always reaches RCM; any open issue
     travels with it as an *RCM exception*.

## The transcript (paste exactly)

```
Doctor: Good morning, William. What brings you in today?
Patient: Good morning, doctor. My sugars have been running high at home, mostly between 220 and 260 in the mornings.
Doctor: How long has that been going on?
Patient: About two months. I ran out of my metformin around then and never got it refilled.
Doctor: Any other symptoms?
Patient: I am thirsty all the time and I get up two or three times at night to pass urine. And for the last few months I have numbness, tingling and burning in both feet, worse at night.
Doctor: Any cuts or sores on your feet?
Patient: No, nothing like that.
Doctor: What medicines are you taking right now?
Patient: Only lisinopril 10 milligrams every morning for my blood pressure. I have not missed it.
Doctor: Any allergies?
Patient: Yes, penicillin. It gives me hives.
Doctor: Anything else I should know?
Patient: My cholesterol was high at my last check-up, but I was never started on anything for it.
Doctor: Let me examine you. Your blood pressure today is 152 over 88 and your pulse is 78. On your foot exam, sensation to the monofilament is decreased in both feet, the skin is intact with no ulcers, and the foot pulses are normal. Before you came in, we collected blood for an A1c, lipid panel and basic metabolic panel to check your kidney function.
Patient: Is that serious?
Doctor: It is something we need to take care of. My assessment today: first, type 2 diabetes, uncontrolled with high sugars, because you have been off metformin. Second, diabetic peripheral neuropathy in both feet. Third, hypertension that is not at goal on lisinopril. Fourth, hyperlipidemia that is not on any treatment.
Patient: So what do we do now?
Doctor: Let me review the clinical decision support for each of these problems before I finalise your treatment plan. I will go through it with you in a moment.
Patient: Okay. Thank you, doctor.
```

## If you read it aloud instead of pasting

Some exact words make the codes come out right. Keep them:

| Say | Why |
|---|---|
| "sugars have been running high", "uncontrolled with high sugars" | Codes E11.65 (diabetes with hyperglycemia), not E11.9 |
| "diabetic peripheral neuropathy" | Codes E11.42 (HCC 37) |
| "hypertension", "cholesterol" / "hyperlipidemia" | Codes I10 and E78.5 |
| "we collected blood for an A1c, lipid panel and basic metabolic panel" | "collected" marks the labs as done today, so they are billed |
| "My assessment today: first ... second ..." | CDS reads the numbered problems and gives one recommendation each |
| "Let me review the clinical decision support ... before I finalise your treatment plan" | Keeps the Plan empty for CDS |

The doctor must **not** say what to start or change (no "restart metformin", no "start a statin").
Otherwise the plan comes from the conversation instead of CDS.

Avoid words that add unwanted codes: fatigue, headache, fever, cough, chest pain, x-ray, smoking,
injection, blood glucose, obesity.

## Mapping to the BRD

| BRD | Where it shows |
|---|---|
| 5.1 Ambient Scribe | S, O and A drafted from the conversation |
| 5.2 CDS, grounded | The Plan is built from per-problem Harrison's recommendations with citations |
| 5.3 Coding & HCC, human gate | Codes and HCCs only on the coder's screen; MEAT check; the coder accepts or overrides |
| 5.6 RCM | Claim scrub, E/M level left to the coder, bill always reaches RCM with exceptions |
| 5.7 VBC | Care gaps: BP control, eye exam, kidney health |
| 5.8 Governance | Draft until signed; clinicians never code; audit trail |
