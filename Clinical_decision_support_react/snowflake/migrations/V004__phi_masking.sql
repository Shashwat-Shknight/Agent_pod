-- =====================================================================
-- V004 - PHI classification and Dynamic Data Masking (Snowflake Enterprise)
--
-- Who sees clear values: the application role (NUUCARE_APP, which applies its own per-user masking,
-- app/domain/masking.py) and the schema owner (NUUCARE_OWNER, migrations). Every other role, including
-- NUUCARE_READONLY (reporting) and ACCOUNTADMIN browsing in Snowsight, sees masked data:
--   names       -> stable pseudonym  PSN-1A2B3C4D5E  (salted SHA-256; counts and joins still work)
--   birth dates -> year only, 90+ aggregated (HIPAA Safe Harbor)
--   MRN / IDs   -> last 4 characters
--   contact     -> redacted (ZIP -> first 3 digits)
--   narrative   -> redacted (conversations, SOAP notes, summaries)
--   PDFs        -> placeholder bytes;  password hashes -> removed
-- Stored data is never changed. To see clear data, a person must use an approved role.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS GOVERNANCE COMMENT = 'PHI classification tags, masking functions and masking policies';

-- ---------------------------------------------------------------------
-- Classification (Snowflake object tagging): find every PHI column with
--   SELECT * FROM TABLE(NUUCARE_DB.INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS('<table>', 'table'));
-- ---------------------------------------------------------------------
CREATE TAG IF NOT EXISTS GOVERNANCE.PHI_CLASS
    ALLOWED_VALUES 'IDENTITY', 'DATE_OF_BIRTH', 'IDENTIFIER', 'CONTACT', 'CLINICAL_NARRATIVE', 'PHI_RECORD', 'PHI_DOCUMENT', 'CREDENTIAL'
    COMMENT = 'HIPAA Safe Harbor identifier family of a column';

-- ---------------------------------------------------------------------
-- Pseudonym salt: generated inside this account at migration time, never in source control.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS GOVERNANCE.MASKING_KEY (SALT STRING NOT NULL, CREATED_AT TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP())
    COMMENT = 'Secret salt for name pseudonyms. Readable only by NUUCARE_OWNER.';
INSERT INTO GOVERNANCE.MASKING_KEY (SALT)
    SELECT RANDSTR(48, RANDOM()) WHERE NOT EXISTS (SELECT 1 FROM GOVERNANCE.MASKING_KEY);
REVOKE ALL PRIVILEGES ON TABLE GOVERNANCE.MASKING_KEY FROM ROLE NUUCARE_READONLY;
REVOKE ALL PRIVILEGES ON TABLE GOVERNANCE.MASKING_KEY FROM ROLE NUUCARE_APP;

CREATE OR REPLACE SECURE FUNCTION GOVERNANCE.MASKING_SALT()
RETURNS STRING
AS
$$
    SELECT MAX(SALT) FROM GOVERNANCE.MASKING_KEY
$$;

-- ---------------------------------------------------------------------
-- JSON walker for VARIANT columns. Key lists mirror app/domain/masking.py (a unit test keeps them equal).
-- ---------------------------------------------------------------------
CREATE OR REPLACE SECURE FUNCTION GOVERNANCE.REDACT_PHI(V VARIANT)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
AS
$$
const NAME_KEYS = new Set(["firstname", "lastname", "legalname", "patientname", "displayname", "subscribername", "membername", "patient"]);
const CONTEXT_NAME_KEYS = new Set(["name"]);
const DOB_KEYS = new Set(["dob", "dateofbirth", "birthdate", "patientdob"]);
const ID_KEYS = new Set(["mrn", "memberid", "groupnumber", "ssn", "subscriberid", "policynumber", "medicaidid", "medicareid"]);
const CONTACT_KEYS = new Set(["mobile", "phone", "homephone", "workphone", "email", "streetaddress", "address", "addressline1", "addressline2", "city", "zip", "zipcode", "postalcode"]);
const NARRATIVE_KEYS = new Set(["transcript", "transcriptdata", "summary", "patientsummary", "soapnote", "soapdraft", "notesummary", "hpi", "keyhighlights", "chiefcomplaint", "subjective", "excerpt", "entries"]);
const PATIENT_MARKERS = new Set(["mrn", "patientid", "ehrpatientid", "dob", "memberid", "legalname"]);
const MASK = "[REDACTED]";
const LIMIT = new Date().getFullYear() - 90;

function norm(key) { return String(key).toLowerCase().replace(/[^a-z0-9]/g, ""); }
function empty(v) { return v === null || v === undefined || v === ""; }
function dob(v) {
    if (empty(v)) return v;
    const m = String(v).match(/(19|20)\d{2}/);
    if (!m) return MASK;
    const year = parseInt(m[0], 10);
    return year <= LIMIT ? LIMIT + " or earlier" : String(year);
}
function ident(v) {
    if (empty(v)) return v;
    const t = String(v);
    return t.length > 4 ? "*".repeat(Math.max(t.length - 4, 4)) + t.slice(-4) : "****";
}
function contact(k, v) {
    if (empty(v)) return v;
    if (k === "zip" || k === "zipcode" || k === "postalcode") {
        const t = String(v);
        return t.length >= 5 ? t.slice(0, 3) + "**" : "*****";
    }
    return MASK;
}
function walk(v) {
    if (Array.isArray(v)) return v.map(walk);
    if (v === null || typeof v !== "object") return v;
    const keys = Object.keys(v);
    const normed = keys.map(norm);
    const aboutPatient = normed.some(function (k) { return PATIENT_MARKERS.has(k); });
    const out = {};
    keys.forEach(function (key, i) {
        const k = normed[i];
        const item = v[key];
        if (NARRATIVE_KEYS.has(k)) out[key] = empty(item) ? item : MASK;
        else if (typeof item === "string" && (NAME_KEYS.has(k) || (CONTEXT_NAME_KEYS.has(k) && aboutPatient))) out[key] = "[NAME]";
        else if (DOB_KEYS.has(k)) out[key] = dob(item);
        else if (ID_KEYS.has(k)) out[key] = ident(item);
        else if (CONTACT_KEYS.has(k)) out[key] = contact(k, item);
        else out[key] = walk(item);
    });
    return out;
}
return walk(V);
$$;

-- ---------------------------------------------------------------------
-- Masking policies
-- ---------------------------------------------------------------------
CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.PHI_NAME AS (VAL STRING) RETURNS STRING ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN VAL IS NULL OR VAL = '' THEN VAL
        ELSE 'PSN-' || UPPER(LEFT(SHA2(LOWER(TRIM(VAL)) || GOVERNANCE.MASKING_SALT()), 10))
    END
    COMMENT = 'Patient name -> stable salted pseudonym';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.PHI_DOB AS (VAL STRING) RETURNS STRING ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN VAL IS NULL OR VAL = '' THEN VAL
        WHEN REGEXP_SUBSTR(VAL, '(19|20)[0-9]{2}') IS NULL THEN '[REDACTED]'
        WHEN TO_NUMBER(REGEXP_SUBSTR(VAL, '(19|20)[0-9]{2}')) <= YEAR(CURRENT_DATE()) - 90
            THEN TO_VARCHAR(YEAR(CURRENT_DATE()) - 90) || ' or earlier'
        ELSE REGEXP_SUBSTR(VAL, '(19|20)[0-9]{2}')
    END
    COMMENT = 'Date of birth -> year only; 90+ aggregated (Safe Harbor)';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.PHI_IDENTIFIER AS (VAL STRING) RETURNS STRING ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN VAL IS NULL OR VAL = '' THEN VAL
        WHEN LENGTH(VAL) <= 4 THEN '****'
        ELSE REPEAT('*', GREATEST(LENGTH(VAL) - 4, 4)) || RIGHT(VAL, 4)
    END
    COMMENT = 'MRN / member IDs -> last four characters';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.PHI_NARRATIVE AS (VAL STRING) RETURNS STRING ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN VAL IS NULL OR VAL = '' THEN VAL
        ELSE '[REDACTED]'
    END
    COMMENT = 'Free text that may identify a patient (conversations, notes, audit details)';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.PHI_JSON AS (VAL VARIANT) RETURNS VARIANT ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN VAL IS NULL THEN VAL
        ELSE GOVERNANCE.REDACT_PHI(VAL)
    END
    COMMENT = 'Application JSON: PHI keys masked, clinical content (codes, vitals, labs) kept';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.PHI_JSON_REDACT AS (VAL VARIANT) RETURNS VARIANT ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN VAL IS NULL THEN VAL
        ELSE TO_VARIANT('[REDACTED]')
    END
    COMMENT = 'JSON that is narrative throughout (transcript turns, raw sheets)';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.PHI_DOCUMENT AS (VAL BINARY) RETURNS BINARY ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN VAL IS NULL THEN VAL
        ELSE TO_BINARY(HEX_ENCODE('[REDACTED PDF]'), 'HEX')
    END
    COMMENT = 'Bill / claim PDFs contain names and dates of birth';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.CREDENTIAL_JSON AS (VAL VARIANT) RETURNS VARIANT ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN IS_OBJECT(VAL) THEN OBJECT_DELETE(VAL, 'password_hash')
        ELSE VAL
    END
    COMMENT = 'Never show password hashes outside the application';

CREATE MASKING POLICY IF NOT EXISTS GOVERNANCE.AUDIT_JSON AS (VAL VARIANT) RETURNS VARIANT ->
    CASE
        WHEN CURRENT_ROLE() IN ('NUUCARE_APP', 'NUUCARE_OWNER') THEN VAL
        WHEN IS_OBJECT(VAL) AND VAL:detail IS NOT NULL THEN OBJECT_INSERT(VAL, 'detail', '[REDACTED]', TRUE)
        ELSE VAL
    END
    COMMENT = 'Audit details can name patients';

-- ---------------------------------------------------------------------
-- Apply: policy + classification tag per column
-- ---------------------------------------------------------------------
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN PATIENT_NAME SET MASKING POLICY GOVERNANCE.PHI_NAME FORCE;
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN PATIENT_DOB  SET MASKING POLICY GOVERNANCE.PHI_DOB FORCE;
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN MRN          SET MASKING POLICY GOVERNANCE.PHI_IDENTIFIER FORCE;
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN RECORD_JSON  SET MASKING POLICY GOVERNANCE.PHI_JSON FORCE;
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN PATIENT_NAME SET TAG GOVERNANCE.PHI_CLASS = 'IDENTITY';
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN PATIENT_DOB  SET TAG GOVERNANCE.PHI_CLASS = 'DATE_OF_BIRTH';
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN MRN          SET TAG GOVERNANCE.PHI_CLASS = 'IDENTIFIER';
ALTER TABLE CLINICAL.ENCOUNTERS MODIFY COLUMN RECORD_JSON  SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';

ALTER TABLE CLINICAL.ENCOUNTER_HISTORY MODIFY COLUMN RECORD_JSON SET MASKING POLICY GOVERNANCE.PHI_JSON FORCE;
ALTER TABLE CLINICAL.ENCOUNTER_HISTORY MODIFY COLUMN RECORD_JSON SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';

ALTER TABLE CLINICAL.ENCOUNTER_TRANSCRIPTS MODIFY COLUMN TRANSCRIPT      SET MASKING POLICY GOVERNANCE.PHI_NARRATIVE FORCE;
ALTER TABLE CLINICAL.ENCOUNTER_TRANSCRIPTS MODIFY COLUMN TRANSCRIPT_DATA SET MASKING POLICY GOVERNANCE.PHI_JSON_REDACT FORCE;
ALTER TABLE CLINICAL.ENCOUNTER_TRANSCRIPTS MODIFY COLUMN TRANSCRIPT      SET TAG GOVERNANCE.PHI_CLASS = 'CLINICAL_NARRATIVE';
ALTER TABLE CLINICAL.ENCOUNTER_TRANSCRIPTS MODIFY COLUMN TRANSCRIPT_DATA SET TAG GOVERNANCE.PHI_CLASS = 'CLINICAL_NARRATIVE';

ALTER TABLE CLINICAL.ENCOUNTER_AI_OUTPUTS MODIFY COLUMN SOAP_DRAFT      SET MASKING POLICY GOVERNANCE.PHI_NARRATIVE FORCE;
ALTER TABLE CLINICAL.ENCOUNTER_AI_OUTPUTS MODIFY COLUMN PATIENT_SUMMARY SET MASKING POLICY GOVERNANCE.PHI_NARRATIVE FORCE;
ALTER TABLE CLINICAL.ENCOUNTER_AI_OUTPUTS MODIFY COLUMN KEY_HIGHLIGHTS  SET MASKING POLICY GOVERNANCE.PHI_JSON_REDACT FORCE;
ALTER TABLE CLINICAL.ENCOUNTER_AI_OUTPUTS MODIFY COLUMN SOAP_DRAFT      SET TAG GOVERNANCE.PHI_CLASS = 'CLINICAL_NARRATIVE';
ALTER TABLE CLINICAL.ENCOUNTER_AI_OUTPUTS MODIFY COLUMN PATIENT_SUMMARY SET TAG GOVERNANCE.PHI_CLASS = 'CLINICAL_NARRATIVE';
ALTER TABLE CLINICAL.ENCOUNTER_AI_OUTPUTS MODIFY COLUMN KEY_HIGHLIGHTS  SET TAG GOVERNANCE.PHI_CLASS = 'CLINICAL_NARRATIVE';

ALTER TABLE CLINICAL.SOAP_NOTES MODIFY COLUMN SOAP_NOTE SET MASKING POLICY GOVERNANCE.PHI_NARRATIVE FORCE;
ALTER TABLE CLINICAL.SOAP_NOTES MODIFY COLUMN SOAP_NOTE SET TAG GOVERNANCE.PHI_CLASS = 'CLINICAL_NARRATIVE';

ALTER TABLE EHR.SHEETS MODIFY COLUMN RECORDS  SET MASKING POLICY GOVERNANCE.PHI_JSON FORCE;
ALTER TABLE EHR.SHEETS MODIFY COLUMN RAW_ROWS SET MASKING POLICY GOVERNANCE.PHI_JSON_REDACT FORCE;
ALTER TABLE EHR.SHEETS MODIFY COLUMN RECORDS  SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';
ALTER TABLE EHR.SHEETS MODIFY COLUMN RAW_ROWS SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';

ALTER TABLE BILLING.GENERATED_BILLS MODIFY COLUMN BILL_JSON    SET MASKING POLICY GOVERNANCE.PHI_JSON FORCE;
ALTER TABLE BILLING.GENERATED_BILLS MODIFY COLUMN BILL_JSON    SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';
ALTER TABLE BILLING.BILL_DOCUMENTS  MODIFY COLUMN PATIENT_NAME SET MASKING POLICY GOVERNANCE.PHI_NAME FORCE;
ALTER TABLE BILLING.BILL_DOCUMENTS  MODIFY COLUMN CONTENT      SET MASKING POLICY GOVERNANCE.PHI_DOCUMENT FORCE;
ALTER TABLE BILLING.BILL_DOCUMENTS  MODIFY COLUMN PATIENT_NAME SET TAG GOVERNANCE.PHI_CLASS = 'IDENTITY';
ALTER TABLE BILLING.BILL_DOCUMENTS  MODIFY COLUMN CONTENT      SET TAG GOVERNANCE.PHI_CLASS = 'PHI_DOCUMENT';
ALTER TABLE BILLING.CLAIMS          MODIFY COLUMN PATIENT_NAME SET MASKING POLICY GOVERNANCE.PHI_NAME FORCE;
ALTER TABLE BILLING.CLAIMS          MODIFY COLUMN CLAIM_JSON   SET MASKING POLICY GOVERNANCE.PHI_JSON FORCE;
ALTER TABLE BILLING.CLAIMS          MODIFY COLUMN PATIENT_NAME SET TAG GOVERNANCE.PHI_CLASS = 'IDENTITY';
ALTER TABLE BILLING.CLAIMS          MODIFY COLUMN CLAIM_JSON   SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';

ALTER TABLE SECURITY.APP_USERS MODIFY COLUMN USER_JSON  SET MASKING POLICY GOVERNANCE.CREDENTIAL_JSON FORCE;
ALTER TABLE SECURITY.APP_USERS MODIFY COLUMN USER_JSON  SET TAG GOVERNANCE.PHI_CLASS = 'CREDENTIAL';
ALTER TABLE SECURITY.AUDIT_LOG MODIFY COLUMN DETAIL     SET MASKING POLICY GOVERNANCE.PHI_NARRATIVE FORCE;
ALTER TABLE SECURITY.AUDIT_LOG MODIFY COLUMN EVENT_JSON SET MASKING POLICY GOVERNANCE.AUDIT_JSON FORCE;
ALTER TABLE SECURITY.AUDIT_LOG MODIFY COLUMN DETAIL     SET TAG GOVERNANCE.PHI_CLASS = 'CLINICAL_NARRATIVE';

ALTER TABLE VBC.STATE_DOCUMENTS MODIFY COLUMN STATE_TEXT SET MASKING POLICY GOVERNANCE.PHI_NARRATIVE FORCE;
ALTER TABLE VBC.STATE_DOCUMENTS MODIFY COLUMN STATE_JSON SET MASKING POLICY GOVERNANCE.PHI_JSON FORCE;
ALTER TABLE VBC.STATE_DOCUMENTS MODIFY COLUMN STATE_TEXT SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';
ALTER TABLE VBC.STATE_DOCUMENTS MODIFY COLUMN STATE_JSON SET TAG GOVERNANCE.PHI_CLASS = 'PHI_RECORD';
