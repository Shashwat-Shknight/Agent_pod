-- =====================================================================
-- V001 - NuuCare core schema
-- Applied by: python -m app.db.migrate   (runs as NUUCARE_DEPLOY_SVC / NUUCARE_OWNER)
--
-- Conventions
--   * Business keys are the IDs the app already uses (PT-xxxxxx, Patient ID, Claim ID...).
--   * Every operational table keeps the full application object in a VARIANT column, so
--     nothing an agent produces is ever lost; hot fields are promoted to typed columns.
--   * PRIMARY KEY / FOREIGN KEY constraints are informational in Snowflake (not enforced);
--     uniqueness is guaranteed by MERGE-based writes in the stored procedures / repositories.
--   * All timestamps are TIMESTAMP_TZ in UTC unless noted.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS CLINICAL COMMENT = 'Consultations: transcripts, AI outputs, SOAP notes, coding';
CREATE SCHEMA IF NOT EXISTS EHR      COMMENT = 'Electronic health record (patient chart sheets)';
CREATE SCHEMA IF NOT EXISTS BILLING  COMMENT = 'Draft bills, bill PDFs, claims';
CREATE SCHEMA IF NOT EXISTS SECURITY COMMENT = 'Application users, role permissions, audit trail';
CREATE SCHEMA IF NOT EXISTS VBC      COMMENT = 'Value-based care workspace state';

-- ---------------------------------------------------------------------
-- CLINICAL
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CLINICAL.ENCOUNTERS (
    ENCOUNTER_ID        STRING        NOT NULL,
    PATIENT_ID          STRING,                    -- EHR Patient ID (record.ehr_patient_id)
    EHR_ENCOUNTER_ID    STRING,
    EHR_NOTE_ID         STRING,
    ENCOUNTER_TYPE      STRING,
    PATIENT_NAME        STRING,
    PATIENT_AGE         STRING,
    PATIENT_GENDER      STRING,
    PATIENT_DOB         STRING,
    MRN                 STRING,
    DOCTOR              STRING,
    STATUS              STRING,
    ASSIGNED_TO         STRING,
    CREATED_BY          STRING,
    ENCOUNTER_AT        TIMESTAMP_NTZ,             -- local clinic time from record date + time
    APPROVAL_DATE       STRING,
    BILL_GENERATED_AT   STRING,
    ANALYSIS_VERSION    STRING,
    RECORD_JSON         VARIANT       NOT NULL,    -- full application record (source of truth)
    RECORD_HASH         STRING        NOT NULL,    -- SHA-256 of canonical JSON, skips no-op writes
    ROW_VERSION         NUMBER(10)    NOT NULL DEFAULT 1,
    IS_DELETED          BOOLEAN       NOT NULL DEFAULT FALSE,
    CREATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY          STRING,
    CONSTRAINT PK_ENCOUNTERS PRIMARY KEY (ENCOUNTER_ID)
)
CHANGE_TRACKING = TRUE
COMMENT = 'One row per consultation. Survives after the patient leaves.';

CREATE TABLE IF NOT EXISTS CLINICAL.ENCOUNTER_HISTORY (
    ENCOUNTER_ID        STRING        NOT NULL,
    ROW_VERSION         NUMBER(10)    NOT NULL,
    RECORD_JSON         VARIANT       NOT NULL,
    RECORD_HASH         STRING        NOT NULL,
    CHANGED_BY          STRING,
    CHANGED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_ENCOUNTER_HISTORY PRIMARY KEY (ENCOUNTER_ID, ROW_VERSION),
    CONSTRAINT FK_ENC_HISTORY FOREIGN KEY (ENCOUNTER_ID) REFERENCES CLINICAL.ENCOUNTERS (ENCOUNTER_ID)
)
COMMENT = 'Append-only version history of every encounter change (clinical audit trail).';

CREATE TABLE IF NOT EXISTS CLINICAL.ENCOUNTER_TRANSCRIPTS (
    ENCOUNTER_ID        STRING        NOT NULL,
    TRANSCRIPT          STRING,
    TRANSCRIPT_DATA     VARIANT,
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_ENC_TRANSCRIPTS PRIMARY KEY (ENCOUNTER_ID),
    CONSTRAINT FK_ENC_TRANSCRIPTS FOREIGN KEY (ENCOUNTER_ID) REFERENCES CLINICAL.ENCOUNTERS (ENCOUNTER_ID)
)
COMMENT = 'Ambient Scribe transcript (raw text + parsed speaker turns).';

CREATE TABLE IF NOT EXISTS CLINICAL.ENCOUNTER_AI_OUTPUTS (
    ENCOUNTER_ID          STRING      NOT NULL,
    SOAP_DRAFT            STRING,
    PATIENT_SUMMARY       STRING,
    KEY_HIGHLIGHTS        VARIANT,
    RETRIEVED_GUIDELINES  STRING,
    RECOMMENDATIONS       STRING,
    MANUAL_INSIGHTS       VARIANT,
    CDS_REQUESTED         BOOLEAN,
    UPDATED_AT            TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_ENC_AI PRIMARY KEY (ENCOUNTER_ID),
    CONSTRAINT FK_ENC_AI FOREIGN KEY (ENCOUNTER_ID) REFERENCES CLINICAL.ENCOUNTERS (ENCOUNTER_ID)
)
COMMENT = 'Ambient Scribe SOAP draft + Clinical Decision Support agent output.';

CREATE TABLE IF NOT EXISTS CLINICAL.ENCOUNTER_CODES (
    ENCOUNTER_ID        STRING        NOT NULL,
    CODE_SYSTEM         STRING        NOT NULL,    -- ICD10 | CPT
    CODE                STRING        NOT NULL,
    EXTRACTED_TERM      STRING,
    DESCRIPTION         STRING,
    DECISION_STATUS     STRING,                    -- Accepted | Rejected | Query sent | NULL (undecided)
    DECIDED_BY          STRING,
    SOURCE              STRING,                    -- agent | ocr_superbill | ...
    DETAILS             VARIANT,
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_ENC_CODES PRIMARY KEY (ENCOUNTER_ID, CODE_SYSTEM, CODE),
    CONSTRAINT FK_ENC_CODES FOREIGN KEY (ENCOUNTER_ID) REFERENCES CLINICAL.ENCOUNTERS (ENCOUNTER_ID)
)
COMMENT = 'Coding agent ICD-10-CM / CPT-HCPCS suggestions and coder decisions.';

CREATE TABLE IF NOT EXISTS CLINICAL.SOAP_NOTES (
    ENCOUNTER_ID        STRING        NOT NULL,
    VERSION             NUMBER(10)    NOT NULL,
    PATIENT_ID          STRING,
    STATUS              STRING        NOT NULL,    -- approved | withdrawn
    SOAP_NOTE           STRING,
    APPROVED_TIMESTAMP  STRING,                    -- as produced by the app (ISO-8601 with offset)
    ACTOR               STRING,
    CREATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_SOAP_NOTES PRIMARY KEY (ENCOUNTER_ID, VERSION)
)
COMMENT = 'Clinician-approved SOAP notes. Append-only: approve/withdraw adds a version.';

-- ---------------------------------------------------------------------
-- EHR  (workbook-shaped chart: Patient_Master, Encounters, Labs, Medications, ...)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS EHR.STORE_METADATA (
    STORE_KEY           STRING        NOT NULL,
    FORMAT              STRING,
    SOURCE_WORKBOOK     STRING,
    SOURCE_CREATED_AT   STRING,
    EXTRA               VARIANT,
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_EHR_META PRIMARY KEY (STORE_KEY)
);

CREATE TABLE IF NOT EXISTS EHR.SHEETS (
    SHEET_NAME          STRING        NOT NULL,
    SHEET_ORDER         NUMBER(5),
    KIND                STRING        NOT NULL,    -- table | raw
    HEADERS             VARIANT,
    RECORDS             VARIANT,                   -- array of row objects (KIND = table)
    RAW_ROWS            VARIANT,                   -- array of arrays     (KIND = raw)
    ROW_COUNT           NUMBER(10),
    CONTENT_HASH        STRING        NOT NULL,
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_EHR_SHEETS PRIMARY KEY (SHEET_NAME)
)
COMMENT = 'EHR chart sheets. Use the EHR.V_* views for row-level, column-typed access.';

-- ---------------------------------------------------------------------
-- BILLING
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS BILLING.GENERATED_BILLS (
    BILL_KEY            STRING        NOT NULL,    -- "<source>:<encounter_id>"
    SOURCE              STRING        NOT NULL,    -- OPD | IPD | ...
    ENCOUNTER_ID        STRING        NOT NULL,
    PATIENT_ID          STRING,
    GROSS_TOTAL         NUMBER(14,2),
    BILL_JSON           VARIANT       NOT NULL,
    CREATED_BY          STRING,
    CREATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_GENERATED_BILLS PRIMARY KEY (BILL_KEY)
)
COMMENT = 'Draft bills released to RCM (input to claim generation).';

CREATE TABLE IF NOT EXISTS BILLING.BILL_DOCUMENTS (
    DOCUMENT_ID         STRING        NOT NULL,    -- UUID
    DOCUMENT_TYPE       STRING        NOT NULL,    -- DRAFT_BILL | CLAIM
    ENCOUNTER_ID        STRING,
    RECORD_ID           STRING,                    -- CLINICAL.ENCOUNTERS.ENCOUNTER_ID
    PATIENT_NAME        STRING,
    REFERENCE_ID        STRING,                    -- Claim ID for claims
    FILE_NAME           STRING        NOT NULL,
    MIME_TYPE           STRING        NOT NULL DEFAULT 'application/pdf',
    SIZE_BYTES          NUMBER(12),
    SHA256              STRING        NOT NULL,
    CONTENT             BINARY        NOT NULL,
    METADATA            VARIANT,                   -- totals, line count, notice, ...
    CREATED_BY          STRING,
    CREATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_BILL_DOCUMENTS PRIMARY KEY (DOCUMENT_ID)
)
COMMENT = 'Rendered PDF bills and claims, stored in the database (no local files).';

CREATE TABLE IF NOT EXISTS BILLING.CLAIMS (
    CLAIM_ID            STRING        NOT NULL,
    ENCOUNTER_ID        STRING,
    CLAIM_TYPE          STRING,                    -- 837P | 837I
    STATUS              STRING,
    PATIENT_NAME        STRING,
    CLAIM_AMOUNT        NUMBER(14,2),
    CLAIM_JSON          VARIANT       NOT NULL,
    VALIDATION_REPORT   VARIANT,
    CREATED_BY          STRING,
    CREATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    IS_DELETED          BOOLEAN       NOT NULL DEFAULT FALSE,
    CONSTRAINT PK_CLAIMS PRIMARY KEY (CLAIM_ID)
)
COMMENT = 'Generated 837P / 837I claim drafts.';

-- ---------------------------------------------------------------------
-- SECURITY
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS SECURITY.APP_USERS (
    USERNAME            STRING        NOT NULL,
    NAME                STRING,
    EMAIL               STRING,
    ROLE                STRING,
    ACTIVE              BOOLEAN,
    USER_JSON           VARIANT       NOT NULL,    -- incl. salted password hash, lockout state
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_APP_USERS PRIMARY KEY (USERNAME)
)
COMMENT = 'Application users. Passwords are stored only as salted PBKDF2 hashes.';

CREATE TABLE IF NOT EXISTS SECURITY.ROLE_PERMISSIONS (
    ROLE                STRING        NOT NULL,
    PERMISSIONS         VARIANT       NOT NULL,
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_ROLE_PERMISSIONS PRIMARY KEY (ROLE)
);

CREATE TABLE IF NOT EXISTS SECURITY.STORE_SETTINGS (
    SETTING_KEY         STRING        NOT NULL,
    SETTING_VALUE       VARIANT,
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_STORE_SETTINGS PRIMARY KEY (SETTING_KEY)
);

CREATE TABLE IF NOT EXISTS SECURITY.AUDIT_LOG (
    EVENT_ID            STRING        NOT NULL,    -- deterministic hash of the event
    EVENT_AT            STRING,                    -- app timestamp
    ACTOR               STRING,
    ACTION              STRING,
    DETAIL              STRING,
    EVENT_JSON          VARIANT,
    INSERTED_AT         TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_AUDIT_LOG PRIMARY KEY (EVENT_ID)
)
COMMENT = 'Append-only user action audit trail (never truncated, unlike the in-app view).';

-- ---------------------------------------------------------------------
-- VBC
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS VBC.STATE_DOCUMENTS (
    DOC_KEY             STRING        NOT NULL,    -- vbc_state | vbc_poc_state
    REVISION            NUMBER(12)    NOT NULL,
    STATE_TEXT          STRING        NOT NULL,    -- exact JSON text (keeps audit hash chain verifiable)
    STATE_JSON          VARIANT       NOT NULL,    -- same document, queryable
    UPDATED_AT          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_VBC_STATE PRIMARY KEY (DOC_KEY)
)
COMMENT = 'Value-based care workspace state (hash-chained audit preserved inside STATE_JSON).';
