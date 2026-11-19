-- =====================================================================
-- V002 - Encounter write procedure + reporting views
-- =====================================================================

-- ---------------------------------------------------------------------
-- CLINICAL.UPSERT_ENCOUNTER
--   One network round trip per save. In a single transaction it:
--     1. skips the write if the record is unchanged (hash compare)
--     2. upserts CLINICAL.ENCOUNTERS (typed columns + full VARIANT)
--     3. appends CLINICAL.ENCOUNTER_HISTORY (new version)
--     4. refreshes ENCOUNTER_TRANSCRIPTS, ENCOUNTER_AI_OUTPUTS, ENCOUNTER_CODES
--   RECORD_TEXT must be canonical JSON (sorted keys) so the hash is stable.
-- ---------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE CLINICAL.UPSERT_ENCOUNTER(RECORD_TEXT STRING, CHANGED_BY STRING)
RETURNS VARIANT
LANGUAGE SQL
COMMENT = 'Transactional upsert of one consultation record and its child tables'
EXECUTE AS OWNER
AS
$$
DECLARE
    enc_id      STRING;
    new_hash    STRING;
    old_hash    STRING;
    old_version NUMBER;
    new_version NUMBER;
BEGIN
    enc_id := PARSE_JSON(RECORD_TEXT):id::STRING;
    IF (enc_id IS NULL OR enc_id = '') THEN
        RETURN OBJECT_CONSTRUCT('status', 'error', 'message', 'record has no id');
    END IF;
    new_hash := SHA2(RECORD_TEXT, 256);

    SELECT MAX(RECORD_HASH), MAX(ROW_VERSION) INTO :old_hash, :old_version
      FROM CLINICAL.ENCOUNTERS WHERE ENCOUNTER_ID = :enc_id;
    IF (old_hash = new_hash) THEN
        RETURN OBJECT_CONSTRUCT('status', 'unchanged', 'encounter_id', enc_id, 'row_version', old_version);
    END IF;
    new_version := COALESCE(old_version, 0) + 1;

    BEGIN TRANSACTION;

    MERGE INTO CLINICAL.ENCOUNTERS t
    USING (SELECT PARSE_JSON(:RECORD_TEXT) AS R) s
       ON t.ENCOUNTER_ID = :enc_id
    WHEN MATCHED THEN UPDATE SET
        PATIENT_ID        = NULLIF(COALESCE(s.R:ehr_patient_id::STRING, s.R:patient_id::STRING), ''),
        EHR_ENCOUNTER_ID  = s.R:ehr_encounter_id::STRING,
        EHR_NOTE_ID       = s.R:ehr_note_id::STRING,
        ENCOUNTER_TYPE    = s.R:ehr_encounter_type::STRING,
        PATIENT_NAME      = s.R:name::STRING,
        PATIENT_AGE       = s.R:age::STRING,
        PATIENT_GENDER    = s.R:gender::STRING,
        PATIENT_DOB       = s.R:dob::STRING,
        MRN               = s.R:mrn::STRING,
        DOCTOR            = s.R:doctor::STRING,
        STATUS            = s.R:status::STRING,
        ASSIGNED_TO       = s.R:assigned_to::STRING,
        CREATED_BY        = s.R:created_by::STRING,
        ENCOUNTER_AT      = TRY_TO_TIMESTAMP_NTZ(s.R:date::STRING || ' ' || COALESCE(s.R:time::STRING, '00:00'), 'DD/MM/YYYY HH24:MI'),
        APPROVAL_DATE     = s.R:approval_date::STRING,
        BILL_GENERATED_AT = s.R:bill_generated_at::STRING,
        ANALYSIS_VERSION  = s.R:analysis_version::STRING,
        RECORD_JSON       = s.R,
        RECORD_HASH       = :new_hash,
        ROW_VERSION       = :new_version,
        IS_DELETED        = FALSE,
        UPDATED_AT        = CURRENT_TIMESTAMP(),
        UPDATED_BY        = :CHANGED_BY
    WHEN NOT MATCHED THEN INSERT (
        ENCOUNTER_ID, PATIENT_ID, EHR_ENCOUNTER_ID, EHR_NOTE_ID, ENCOUNTER_TYPE, PATIENT_NAME, PATIENT_AGE,
        PATIENT_GENDER, PATIENT_DOB, MRN, DOCTOR, STATUS, ASSIGNED_TO, CREATED_BY, ENCOUNTER_AT, APPROVAL_DATE,
        BILL_GENERATED_AT, ANALYSIS_VERSION, RECORD_JSON, RECORD_HASH, ROW_VERSION, UPDATED_BY
    ) VALUES (
        :enc_id,
        NULLIF(COALESCE(s.R:ehr_patient_id::STRING, s.R:patient_id::STRING), ''),
        s.R:ehr_encounter_id::STRING, s.R:ehr_note_id::STRING, s.R:ehr_encounter_type::STRING,
        s.R:name::STRING, s.R:age::STRING, s.R:gender::STRING, s.R:dob::STRING, s.R:mrn::STRING,
        s.R:doctor::STRING, s.R:status::STRING, s.R:assigned_to::STRING, s.R:created_by::STRING,
        TRY_TO_TIMESTAMP_NTZ(s.R:date::STRING || ' ' || COALESCE(s.R:time::STRING, '00:00'), 'DD/MM/YYYY HH24:MI'),
        s.R:approval_date::STRING, s.R:bill_generated_at::STRING, s.R:analysis_version::STRING,
        s.R, :new_hash, :new_version, :CHANGED_BY
    );

    INSERT INTO CLINICAL.ENCOUNTER_HISTORY (ENCOUNTER_ID, ROW_VERSION, RECORD_JSON, RECORD_HASH, CHANGED_BY)
    SELECT :enc_id, :new_version, PARSE_JSON(:RECORD_TEXT), :new_hash, :CHANGED_BY;

    MERGE INTO CLINICAL.ENCOUNTER_TRANSCRIPTS t
    USING (SELECT PARSE_JSON(:RECORD_TEXT) AS R) s
       ON t.ENCOUNTER_ID = :enc_id
    WHEN MATCHED THEN UPDATE SET
        TRANSCRIPT = s.R:transcript::STRING, TRANSCRIPT_DATA = s.R:transcript_data, UPDATED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (ENCOUNTER_ID, TRANSCRIPT, TRANSCRIPT_DATA)
        VALUES (:enc_id, s.R:transcript::STRING, s.R:transcript_data);

    MERGE INTO CLINICAL.ENCOUNTER_AI_OUTPUTS t
    USING (SELECT PARSE_JSON(:RECORD_TEXT) AS R) s
       ON t.ENCOUNTER_ID = :enc_id
    WHEN MATCHED THEN UPDATE SET
        SOAP_DRAFT = s.R:summary::STRING, PATIENT_SUMMARY = s.R:patient_summary::STRING,
        KEY_HIGHLIGHTS = s.R:key_highlights, RETRIEVED_GUIDELINES = s.R:retrieved_guidelines::STRING,
        RECOMMENDATIONS = s.R:recommendations::STRING, MANUAL_INSIGHTS = s.R:manual_insights,
        CDS_REQUESTED = s.R:cds_requested::BOOLEAN, UPDATED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (
        ENCOUNTER_ID, SOAP_DRAFT, PATIENT_SUMMARY, KEY_HIGHLIGHTS, RETRIEVED_GUIDELINES,
        RECOMMENDATIONS, MANUAL_INSIGHTS, CDS_REQUESTED
    ) VALUES (
        :enc_id, s.R:summary::STRING, s.R:patient_summary::STRING, s.R:key_highlights,
        s.R:retrieved_guidelines::STRING, s.R:recommendations::STRING, s.R:manual_insights,
        s.R:cds_requested::BOOLEAN
    );

    DELETE FROM CLINICAL.ENCOUNTER_CODES WHERE ENCOUNTER_ID = :enc_id;
    INSERT INTO CLINICAL.ENCOUNTER_CODES (
        ENCOUNTER_ID, CODE_SYSTEM, CODE, EXTRACTED_TERM, DESCRIPTION, DECISION_STATUS, DECIDED_BY, SOURCE, DETAILS
    )
    SELECT :enc_id, c.CODE_SYSTEM, c.CODE, c.TERM, c.DESCRIPTION,
           GET(c.R:code_decisions, c.DECISION_KEY):status::STRING,
           GET(c.R:code_decisions, c.DECISION_KEY):by::STRING,
           c.SOURCE, c.DETAILS
      FROM (
        SELECT s.R, 'ICD10' AS CODE_SYSTEM,
               TRIM(f.value:"ICD-10 Code"::STRING)           AS CODE,
               f.value:"Extracted Condition"::STRING         AS TERM,
               f.value:"Matched Disease/Injury"::STRING      AS DESCRIPTION,
               'ICD:' || TRIM(f.value:"ICD-10 Code"::STRING) AS DECISION_KEY,
               COALESCE(f.value:source::STRING, 'agent')     AS SOURCE,
               f.value                                       AS DETAILS
          FROM (SELECT PARSE_JSON(:RECORD_TEXT) AS R) s,
               LATERAL FLATTEN(INPUT => s.R:code_results:icd10) f
        UNION ALL
        SELECT s.R, 'CPT',
               TRIM(f.value:"CPT/HCPCS Code"::STRING),
               f.value:"Extracted Procedure"::STRING,
               f.value:"Matched Procedure/Service"::STRING,
               'CPT:' || TRIM(f.value:"CPT/HCPCS Code"::STRING),
               COALESCE(f.value:source::STRING, 'agent'),
               f.value
          FROM (SELECT PARSE_JSON(:RECORD_TEXT) AS R) s,
               LATERAL FLATTEN(INPUT => s.R:code_results:cpt) f
      ) c
     WHERE c.CODE IS NOT NULL AND c.CODE <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.CODE_SYSTEM, c.CODE ORDER BY c.CODE) = 1;

    COMMIT;
    RETURN OBJECT_CONSTRUCT('status', 'saved', 'encounter_id', enc_id, 'row_version', new_version);
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- ---------------------------------------------------------------------
-- CLINICAL views
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW CLINICAL.V_LATEST_SOAP_NOTES
COMMENT = 'Current SOAP note state per encounter (latest version)'
AS
SELECT *
  FROM CLINICAL.SOAP_NOTES
QUALIFY ROW_NUMBER() OVER (PARTITION BY ENCOUNTER_ID ORDER BY VERSION DESC) = 1;

CREATE OR REPLACE VIEW CLINICAL.V_PATIENT_HISTORY
COMMENT = 'Every consultation per patient with AI outputs and the approved SOAP note'
AS
SELECT e.PATIENT_ID,
       e.PATIENT_NAME,
       e.ENCOUNTER_ID,
       e.ENCOUNTER_AT,
       e.ENCOUNTER_TYPE,
       e.DOCTOR,
       e.STATUS,
       ai.PATIENT_SUMMARY,
       ai.RECOMMENDATIONS,
       IFF(s.STATUS = 'approved', s.SOAP_NOTE, NULL) AS APPROVED_SOAP_NOTE,
       IFF(s.STATUS = 'approved', s.APPROVED_TIMESTAMP, NULL) AS APPROVED_AT,
       e.ROW_VERSION,
       e.UPDATED_AT
  FROM CLINICAL.ENCOUNTERS e
  LEFT JOIN CLINICAL.ENCOUNTER_AI_OUTPUTS ai ON ai.ENCOUNTER_ID = e.ENCOUNTER_ID
  LEFT JOIN CLINICAL.V_LATEST_SOAP_NOTES s  ON s.ENCOUNTER_ID  = e.ENCOUNTER_ID
 WHERE NOT e.IS_DELETED;

CREATE OR REPLACE VIEW CLINICAL.V_CODING_SUMMARY
COMMENT = 'Code counts and decisions per encounter'
AS
SELECT ENCOUNTER_ID,
       COUNT_IF(CODE_SYSTEM = 'ICD10')                                   AS ICD10_CODES,
       COUNT_IF(CODE_SYSTEM = 'CPT')                                     AS CPT_CODES,
       COUNT_IF(DECISION_STATUS = 'Accepted')                            AS ACCEPTED,
       COUNT_IF(DECISION_STATUS = 'Rejected')                            AS REJECTED,
       COUNT_IF(DECISION_STATUS IS NULL)                                 AS UNDECIDED
  FROM CLINICAL.ENCOUNTER_CODES
 GROUP BY ENCOUNTER_ID;

-- ---------------------------------------------------------------------
-- EHR views: row-level, typed access to the chart sheets
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW EHR.V_SHEET_ROWS
COMMENT = 'Every EHR row from every table sheet, one row per record'
AS
SELECT s.SHEET_NAME,
       f.index                       AS ROW_INDEX,
       f.value:"Patient_ID"::STRING  AS PATIENT_ID,
       f.value                       AS ROW_DATA
  FROM EHR.SHEETS s,
       LATERAL FLATTEN(INPUT => s.RECORDS) f
 WHERE s.KIND = 'table';

CREATE OR REPLACE VIEW EHR.V_PATIENTS AS
SELECT ROW_DATA:"Patient_ID"::STRING          AS PATIENT_ID,
       ROW_DATA:"MRN"::STRING                 AS MRN,
       ROW_DATA:"First_Name"::STRING          AS FIRST_NAME,
       ROW_DATA:"Last_Name"::STRING           AS LAST_NAME,
       ROW_DATA:"Legal_Name"::STRING          AS LEGAL_NAME,
       TRY_TO_DATE(LEFT(ROW_DATA:"DOB"::STRING, 10)) AS DOB,
       TRY_TO_NUMBER(ROW_DATA:"Age"::STRING)  AS AGE,
       ROW_DATA:"Sex_at_Birth"::STRING        AS SEX_AT_BIRTH,
       ROW_DATA:"Preferred_Language"::STRING  AS PREFERRED_LANGUAGE,
       ROW_DATA                               AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Patient_Master';

CREATE OR REPLACE VIEW EHR.V_ENCOUNTERS AS
SELECT ROW_DATA:"Encounter_ID"::STRING          AS ENCOUNTER_ID,
       PATIENT_ID,
       TRY_TO_DATE(LEFT(ROW_DATA:"Date"::STRING, 10)) AS ENCOUNTER_DATE,
       ROW_DATA:"Type"::STRING                  AS ENCOUNTER_TYPE,
       ROW_DATA:"Department"::STRING            AS DEPARTMENT,
       ROW_DATA:"Provider"::STRING              AS PROVIDER,
       ROW_DATA:"Status"::STRING                AS STATUS,
       ROW_DATA:"Primary_Diagnosis_Code"::STRING AS PRIMARY_DIAGNOSIS_CODE,
       ROW_DATA:"Primary_Diagnosis"::STRING     AS PRIMARY_DIAGNOSIS,
       ROW_DATA                                 AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Encounters';

CREATE OR REPLACE VIEW EHR.V_PROBLEMS AS
SELECT ROW_DATA:"Problem_ID"::STRING AS PROBLEM_ID, PATIENT_ID,
       ROW_DATA:"ICD_10_CM"::STRING AS ICD_10_CM, ROW_DATA:"Problem_Name"::STRING AS PROBLEM_NAME,
       ROW_DATA:"Clinical_Status"::STRING AS CLINICAL_STATUS, ROW_DATA AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Problems';

CREATE OR REPLACE VIEW EHR.V_MEDICATIONS AS
SELECT ROW_DATA:"Medication_ID"::STRING AS MEDICATION_ID, PATIENT_ID,
       ROW_DATA:"Medication"::STRING AS MEDICATION, ROW_DATA:"Dose"::STRING AS DOSE,
       ROW_DATA:"Frequency"::STRING AS FREQUENCY, ROW_DATA:"Status"::STRING AS STATUS, ROW_DATA AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Medications';

CREATE OR REPLACE VIEW EHR.V_ALLERGIES AS
SELECT ROW_DATA:"Allergy_ID"::STRING AS ALLERGY_ID, PATIENT_ID,
       ROW_DATA:"Substance"::STRING AS SUBSTANCE, ROW_DATA:"Reaction"::STRING AS REACTION,
       ROW_DATA:"Severity"::STRING AS SEVERITY, ROW_DATA AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Allergies';

CREATE OR REPLACE VIEW EHR.V_LABS AS
SELECT ROW_DATA:"Lab_ID"::STRING AS LAB_ID, PATIENT_ID,
       TRY_TO_DATE(LEFT(ROW_DATA:"Result_Date"::STRING, 10)) AS RESULT_DATE,
       ROW_DATA:"LOINC_Code"::STRING AS LOINC_CODE, ROW_DATA:"Test_Name"::STRING AS TEST_NAME,
       ROW_DATA:"Result"::STRING AS RESULT, ROW_DATA:"Unit"::STRING AS UNIT,
       ROW_DATA:"Flag"::STRING AS FLAG, ROW_DATA AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Labs';

CREATE OR REPLACE VIEW EHR.V_VITALS AS
SELECT ROW_DATA:"Vital_ID"::STRING AS VITAL_ID, PATIENT_ID,
       TRY_TO_DATE(LEFT(ROW_DATA:"Observed_Date"::STRING, 10)) AS OBSERVED_DATE,
       TRY_TO_NUMBER(ROW_DATA:"Systolic_mmHg"::STRING)  AS SYSTOLIC_MMHG,
       TRY_TO_NUMBER(ROW_DATA:"Diastolic_mmHg"::STRING) AS DIASTOLIC_MMHG,
       TRY_TO_NUMBER(ROW_DATA:"Heart_Rate_bpm"::STRING) AS HEART_RATE_BPM,
       TRY_TO_DOUBLE(ROW_DATA:"BMI"::STRING)            AS BMI,
       ROW_DATA AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Vitals';

CREATE OR REPLACE VIEW EHR.V_BILLING_CLAIMS AS
SELECT ROW_DATA:"Claim_ID"::STRING AS CLAIM_ID, PATIENT_ID,
       ROW_DATA:"Encounter_ID"::STRING AS ENCOUNTER_ID,
       ROW_DATA:"CPT_HCPCS"::STRING AS CPT_HCPCS,
       TRY_TO_DOUBLE(ROW_DATA:"Charge_USD"::STRING) AS CHARGE_USD,
       ROW_DATA:"Payer"::STRING AS PAYER, ROW_DATA:"Claim_Status"::STRING AS CLAIM_STATUS,
       ROW_DATA AS ROW_DATA
  FROM EHR.V_SHEET_ROWS WHERE SHEET_NAME = 'Billing_Claims';

-- ---------------------------------------------------------------------
-- BILLING / SECURITY views
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW BILLING.V_BILL_DOCUMENTS
COMMENT = 'PDF catalogue without the binary payload'
AS
SELECT DOCUMENT_ID, DOCUMENT_TYPE, ENCOUNTER_ID, RECORD_ID, PATIENT_NAME, REFERENCE_ID,
       FILE_NAME, SIZE_BYTES, SHA256, METADATA, CREATED_BY, CREATED_AT
  FROM BILLING.BILL_DOCUMENTS;

CREATE OR REPLACE VIEW SECURITY.V_AUDIT_RECENT AS
SELECT EVENT_AT, ACTOR, ACTION, DETAIL, INSERTED_AT
  FROM SECURITY.AUDIT_LOG
 WHERE INSERTED_AT >= DATEADD(day, -30, CURRENT_TIMESTAMP());
