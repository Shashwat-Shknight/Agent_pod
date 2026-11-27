# NuuCare agents: architecture review

**Scope:** every agent in the Streamlit platform:
- Ambient Scribe
- Clinical Decision Support (RAG)
- Diagnostic orders
- Prior authorization
- Coding & HCC
- Claim scrubbing and generation
- Revenue cycle (eligibility → adjudication → A/R)
- Care gaps

**Question asked:** where is behaviour hard-coded that should be data, and what must stay in code?

## Principle applied

| Kind of logic | Where it belongs | Why |
|---|---|---|
| **Policy**: payer contracts, plan designs, federal limits, test catalogs, code maps, edit tables, thresholds | **Rule packs** (versioned JSON, `app/rules/packs/`), or learned from the hospital's own data | Changes yearly or by site. Analysts own it, not engineers. |
| **Algorithms**: negation detection, cost-share order, aging arithmetic, hierarchy application | Code, with unit tests | Changing them is a software change that needs review. |
| **Standards**: CPT E/M code ranges, X12 segment layout, CARC meanings | Code | Stable, external definitions. |
| **Deployment choices**: LLM temperature, retrieval depth, payer connector | `.env` | They differ per environment. |

Each pack carries `version`, `effective` date, `owner` and `sources`. It is re-read when the file changes, so no restart is needed. A site can override any pack by pointing `RULES_DIR` at a folder, without a code change. **Oversight → Policy & rule packs in use** shows what is live.

## Findings and what changed

| # | Agent | Finding | Change |
|---|---|---|---|
| 1 | RCM | Payer contracts, plan designs, statement terms, aging buckets and priority thresholds were Python constants | `rcm_policy` pack, with plan-year sections |
| 2 | RCM | Contract rates were guessed per payer | **Learned from the hospital chargemaster** (expected allowed ÷ gross over its charge lines, per payer and per CPT). The pack is the fallback when a payer is not in the chargemaster. |
| 3 | RCM | Original Medicare used the synthetic coverage deductible | Federal Part B deductible per plan year: **$283 for 2026**, $257 for 2025, 20% coinsurance, no copay, no out-of-pocket cap |
| 4 | RCM | Out-of-pocket maximums were unbounded plan guesses | Commercial plans are capped at the **ACA limit ($10,600 for 2026)**. Medicare Advantage is capped at the **in-network MOOP ($9,250)**. Medicaid uses the nominal copay ($4). |
| 5 | Diagnostics | 36-test catalog (CPT, LOINC, ranges, repeat intervals, indications) was in code | `diagnostic_catalog` pack. Informatics can add a test or change an interval. |
| 6 | Prior auth | CPT ranges were in code; the hospital's own CDM flag was ignored | `prior_auth` pack plus the **CDM "Prior Auth Flag"** column. Exempt payer classes and auth validity are configurable. |
| 7 | Coding & HCC | Ten-code demo map, no V28 hierarchies, so 37 and 38 were double-counted | `hcc_v28` pack with 103 common chronic codes, labels and **hierarchies** (37>38, 224>226, 326>327>328>329...). It also has an importer for the **official CMS mapping**. |
| 8 | Claims | Medical-necessity, NCCI and MUE edits lived in two places that could disagree | One `claim_edits` pack, used by both the expected adjudication and the claim scrubber |
| 9 | Coding / CDS | Symptom lexicon and retrieval query expansions were in code | `clinical_lexicon` pack |
| 10 | Care gaps | HbA1c goal, BP goal, look-back and eye/kidney rules were in code | `care_gaps` pack (HEDIS GSD / EED / KED / CBP references) |
| 11 | Claims | Sample NPIs and payer list were in code. **Professional claims used place of service 11 (office) and outpatient facility claims used type of bill 111 (inpatient).** | `providers` pack: provider directory, place of service **22** (hospital outpatient), type of bill **131** (outpatient) / 111 (inpatient). The 837 CLM05 is corrected to match. |
| 12 | Scribe / CDS | LLM temperature and retrieval depth were fixed in code | `.env`: `SCRIBE_TEMPERATURE`, `LLM_TEMPERATURE`, `CDS_MAX_PROBLEMS`, `CDS_PASSAGES_PER_PROBLEM`, `CDS_SUMMARY_TOP_K` |
| 13 | RCM / Prior auth | Payer behaviour (eligibility, auth decision, 277CA, 835) was mixed into workflow code | **`PayerGateway` interface** with a `simulated` connector. Set `PAYER_GATEWAY` to choose a real clearinghouse connector once one is built. |
| 14 | RCM | A claim could be submitted, or a remittance posted, twice | Idempotency guards: submit only from *Ready / Rejected*, post an 835 only on an *Accepted* claim |
| 15 | Coding | "Denies chest pain" and "Any chest pain?" were coded as R07.9 | Negation, family-history, risk and question handling |
| 16 | All screens | Internal EHR key (P100067) was shown to users | MRN everywhere, plus `ENC#########` encounter numbers linking every artifact |

## Intentionally still in code

- Negation and clause rules, the cost-share order (copay → deductible → coinsurance → out-of-pocket cap) and the hierarchy algorithm.
- E/M code range 99202–99215 and MDM levels, X12 segment structure, and the CARC code semantics. Their display text is in the pack.
- ID formats (MRN series, ENC/ORD/LAB/PA numbering) and sheet names. These are system design, and changing them needs a data migration.

## Remaining challenges for production

| Area | Today | Recommendation |
|---|---|---|
| Data model | EHR sheets are JSON documents in `EHR.SHEETS`, and a change rewrites the whole sheet | Move orders, prior auths, accounts and claim lines to relational tables. The V006 views are the bridge. This removes write contention at scale. |
| Payer connectivity | Simulated gateway | Build a clearinghouse connector: X12 270/271, 278 or HL7 Da Vinci PAS, and 837 / 277CA / 835. CMS-0057-F requires payer prior-authorization APIs from 2027, so plan for FHIR PAS. |
| HCC coverage | 103-code seed | Import the official 2026 CMS mapping (about 8,000 codes) with `python -m app.scripts.import_hcc_mappings <file>`. CMS blocks automated downloads, so download it in a browser. |
| Claim edits | Sample NCCI / MUE / LCD rules | Load the quarterly CMS NCCI and MUE files and payer LCD/NCD policies into `claim_edits` |
| Allowed amounts | Ratio of charges | Payer fee schedules: Medicare PFS / OPPS APC rates, and commercial contract files |
| Reference ranges | Adult defaults | Lab-, age- and sex-specific ranges from the performing lab |
| LLM governance | Prompts in code, no evaluation set | Version the prompts, build a scribe/CDS evaluation set, and confirm a BAA with the LLM provider for PHI |
| Privacy | PHI masking exists only in the React copy | Port role-based masking to Streamlit if Streamlit is the deliverable |
| Time | Naive local timestamps | Store UTC and render in the facility time zone |

## Sources for the 2026 values

- CMS, *2026 Medicare Parts A & B Premiums and Deductibles*: Part B deductible $283.
- HHS, 2026 maximum annual limitation on cost sharing (revised June 2025): $10,600 self-only / $21,200 family.
- KFF, *Medicare Advantage Out-of-Pocket Limits* (2026): in-network MOOP $9,250.
- 42 CFR 447.52: Medicaid nominal cost sharing.
- Published CMS-HCC V28 references for the HF, CKD, depression, COPD and AFib categories.
