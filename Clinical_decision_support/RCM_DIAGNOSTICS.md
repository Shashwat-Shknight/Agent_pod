# Diagnostics, prior authorization and revenue cycle

This branch fixes four gaps in the consultation-to-cash flow.

| Gap | What happens now |
|---|---|
| Doctors rarely say "I'll order an HbA1c" out loud, so tests never reached the note, the EHR or the bill | **Diagnostic orders** card on the CDS workbench proposes the tests the assessment, CDS recommendations and chart imply, with a reason and a due / not-due check |
| Patients showed as `P100067`, visits as random `ENC-3F9A…` / `PT-123456` | **MRN** is the patient identifier on every screen; each visit gets a hospital encounter number **ENC#########** that the consultation, orders, results, authorizations, claims and patient account all share |
| Prior authorization was a static list | Runs **in the background** when a gated study is ordered; the clinician sees only a small badge, and billing holds the line until it is approved |
| Billing stopped at gross charges; claims had no payer, member ID or diagnosis pointers | **RCM engine**: eligibility and benefits, expected adjudication, patient account, 837 → 277CA → 835, statement, aging, next best action, KPIs |

## Product workflow: who does what, and what is enforced

| Step | Who | Screen | What happens | Gate |
|---|---|---|---|---|
| 1 Registration | Front desk | Consultations | Patient (MRN), primary insurance (payer, plan, member ID) and an encounter `ENC#########` are created in the EHR | First and last name; DOB for a new patient |
| 2 Ambient scribe | Clinician | Consultations → Record | Speech → transcript → SOAP draft | — |
| 3 CDS and orders | Clinician | Clinical Review & CDS | Harrison's-grounded recommendations. Tests are **ordered** (Routine / STAT) and nothing is marked performed. Prior authorization runs **silently** for gated studies, and only a status badge shows. | Orders only before signing |
| 4 Sign | Clinician | SOAP Note & Sign | The note is authenticated and goes to the coding queue | Clinical Review · Approve |
| 5 Coding & HCC | Coder | Coding & HCC | ICD-10-CM, CPT and V28 HCC with hierarchies; ordered diagnostics are shown with their codes | **Codes can be accepted only on a signed note** |
| 6 Charge capture | RCM / ancillary | Billing & RCM | Confirm what was **performed / collected** and record results; confirmed services join the bill | RCM Workspace · Edit |
| 7 Bill and estimate | RCM | Billing & RCM | Eligibility, expected allowed amount, patient and payer split, denial risks | **Signed note + at least one accepted diagnosis + attestation** |
| 8 Claims | RCM | Claim Generation | 837P/837I with MRN, member ID, per-line diagnosis pointers and auth number. Then submit (277CA) and payer remittance (835). | Submit once; post a remittance only on an accepted claim |
| 9 Patient A/R | RCM | Patient A/R | Statement, aging, priority, next action, payments, financial assistance | — |

**System of record.** The EHR in Snowflake holds orders, results, prior authorizations, benefits, patient accounts and claim lines. CDS and Billing reconcile with it silently each time they open. This picks up results from charge capture and payer decisions from the Prior Authorization page, and it restores any order whose save was interrupted.

**Stability.** Pooled Snowflake sessions are retired before their four-hour token expires. An expired session is re-authenticated automatically instead of returning "Database unavailable".

## Also fixed

- **MRN on every screen.** These now show the MRN, never the internal EHR key (P100067):
  - consultation list
  - billing patient list and banner
  - claim screens
  - prior auth and medication pickers
  - EHR Records
  - Oversight
  - the coding "Patient linked to EHR" check

  Older consultations that never stored an MRN get it from the EHR.
- **Negated and asked-about symptoms are not coded.** Neither "denies chest pain" nor the doctor's question "Any chest pain?" becomes R07.9. Family history and "at risk of" are not coded either. The detector was ported from the React copy and extended for questions.
- **Common problems get a code** when the fuzzy matcher misses them: depression, anxiety, osteoarthritis, obesity, fatigue.
- **Claims open ready to generate.** The rendering provider defaults to the attending clinician, and payer and member ID come from coverage. Before, empty provider fields silently blocked "Generate claims".
- **No over-ordering.** Non-specific findings such as fatigue suggest a test without pre-ticking it.
- **The 837 file** uses the payer member ID as the subscriber ID, gives each line its own diagnosis pointers, and carries the authorization number (REF*G1).
- **Value-based-care trend** counts verified evidence for the latest point, the same way the compliance rate does.

## Walkthrough (demo)

1. **Consultations**: pick a patient (shown as *Name · MRN · DOB*). On save, the EHR assigns the encounter number (for example ENC700100001), and the consultation uses it as its ID.
2. **Clinical Review & CDS → Diagnostic orders**:
   - Suggested tests are pre-ticked when the evidence is strong *and* the test is due. The frequency guard says, for example, "Not due · last 7.9% on 01 Oct 2026; repeat from 30 Dec 2026".
   - **Place orders** saves them to the EHR (`ORD########`) and adds a plain-language line to the Plan. The line names the test and the reason, never a code.
   - A study that needs payer approval (for example an MRI brain on a commercial plan) is submitted automatically as an ePA request (`PA########`). A supported diagnosis is approved with an authorization number. An unsupported one is pended.
   - After the visit, record specimen collection or the result. A resulted lab is written to the EHR Labs sheet with a flag (High / Low / Normal), so the next visit's due check sees it.
3. **Billing & RCM**:
   - Performed or resulted orders become billable lines, and one venipuncture (36415) is added when blood was drawn.
   - Ordered-only tests are held as *PENDING*. A study without an approved authorization is held as *HELD* (CARC 197 risk).
   - **Eligibility & expected reimbursement** shows the 271-style benefits (copay, deductible left, coinsurance, out-of-pocket left, contract rate). For each line it shows allowed, contractual adjustment, patient share (copay / deductible / coinsurance) and payer expected, with CARC codes.
   - Enter what was collected at check-out, then **Send to RCM**. This opens the patient account and saves the benefits snapshot. It also stamps the encounter's primary diagnosis.
4. **Claim Generation**:
   - Claims now carry MRN, payer, member ID, group, diagnosis pointers per line and the prior-authorization number (loop 2300 REF*G1).
   - The **Revenue cycle** card runs **Submit (837)**, and the clearinghouse acceptance (277CA) is checked on the spot.
   - **Post payer remittance (835)** pays the payer share, moves the member's accumulators and sends the first patient statement (due in 30 days).
5. **Patient A/R** (new page under RCM tools):
   - KPIs, A/R by aging bucket, and a follow-up worklist ordered by priority.
   - Post patient payments or financial assistance there.
   - Accounts from the chart's existing claim history are included, so aging is populated from day one.

## Rules the engine applies

- **Cost share order**:
  - Office visit: one copay (PR-3).
  - Other services: deductible first (PR-1), then coinsurance (PR-2).
  - The total is capped at the out-of-pocket maximum.
  - Preventive services (screening mammogram, LDCT, HCV screening, PHQ-9, preventive visits): $0 under ACA.
- **Payer classes**:
  - Original Medicare: Part B deductible + 20%, no copay, no out-of-pocket cap, no prior auth for these studies.
  - Medicare Advantage and Commercial: plan copay / deductible / coinsurance.
  - Medicaid: nominal copay (≤ $4), no deductible.
- **Denials instead of patient bills**:
  - Missing authorization: CO-197.
  - Diagnosis does not support the service: CO-50 (sample LCD-style rules).
  - Coverage inactive on the date of service: CO-27.
  - In every case the patient is not billed. The account goes to denial work.
- **Accumulators**:
  - Seeded from the chart's paid claims this plan year.
  - After every posted 835, *Deductible_Met_After* / *OOP_Met_After* feed the next visit's eligibility.
- **Aging**: Not yet billed → With payer → Statement pending → Current → 1-30 → 31-60 → 61-90 → 90+.
- **Next best action**:
  - Reminder, then call with a payment plan, then financial-assistance screening and final notice.
  - No collection action before day 120, per IRS 501(r).
- Contract rates and payer responses are **synthetic** and labelled as such in the UI.

## Where the data lives in Snowflake

Everything goes through the FastAPI backend into `EHR.SHEETS`, one row per sheet. The backend assigns the IDs under its store lock.

| Sheet | One row per | Key |
|---|---|---|
| Encounters | visit (now with MRN, primary diagnosis, status) | Encounter_ID `ENC#########` |
| Orders_Procedures | diagnostic order | Order_ID `ORD########` |
| Labs | resulted lab test | Lab_ID `LAB########` |
| Prior_Authorizations | authorization request | PA_ID `PA########` |
| Payer_Plan_Benefits | eligibility snapshot per encounter | Benefit_Key = Encounter_ID |
| Patient_Balance | patient account per encounter | Account_ID = Encounter_ID |
| Billing_Claims | adjudicated claim line (`Source = NuuCare RCM`) | Claim_ID |

Claims stay in `BILLING.CLAIMS`. Their JSON now includes `Lifecycle` (submitted / 277CA / 835 events) and `Remittance` (payer claim control number, paid amount, line CARCs).

Optional typed views (migration **V006**) for Snowsight:
- `EHR.V_DIAGNOSTIC_ORDERS`
- `EHR.V_PRIOR_AUTHORIZATIONS`
- `BILLING.V_PAYER_PLAN_BENEFITS`
- `BILLING.V_PATIENT_BALANCE` (live days past due and aging bucket)
- `BILLING.V_PATIENT_AR_AGING`

## Code map

| File | Role |
|---|---|
| `frontend/diagnostics.py` | Test catalog (CPT, LOINC, indications, frequency, prior-auth flag), suggestion engine, order lifecycle, billing lines |
| `frontend/prior_auth.py` | Authorization rules by service and payer class, request builder, simulated payer decision |
| `frontend/rcm.py` | Eligibility, benefits, adjudication, accounts, aging, next action, KPIs (pure functions) |
| `frontend/orders_service.py`, `frontend/rcm_service.py` | Saving all of the above to the EHR through the API |
| `frontend/views/orders_panel.py`, `rcm_lifecycle.py`, `patient_ar.py` | The new cards and page |
| `data/supplemental_cdm.json` | Prices for codes the hospital chargemaster lacks (99202-99205, 36415, ECG, TSH, CMP, …); the hospital's own price always wins |
| `app/services/ehr_json_service.py` | MRN series, sequential IDs, new-sheet creation, upsert, per-sheet reads |
