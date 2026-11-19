-- =====================================================================
-- V003 - Official medical code sets, versioned by effective date
--
--   ICD-10-CM  : CDC/NCHS official release files (annual Oct 1 + April updates)
--   HCPCS L-II : CMS HCPCS file, via the NLM Clinical Tables API
--   CPT        : AMA-licensed - not loaded (no license); codes pass through unverified
--
-- Each code version is a row with VALID_FROM / VALID_TO (type-2 history), so a claim can be
-- checked against the code set that was in force on its date of service.
-- Loaded by:  python -m app.reference.sync   (and automatically by the API scheduler)
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS REFERENCE COMMENT = 'Official medical code sets (ICD-10-CM, HCPCS Level II), versioned';

CREATE TABLE IF NOT EXISTS REFERENCE.CODE_SET_RELEASES (
    CODE_SYSTEM       STRING        NOT NULL,     -- ICD10CM | HCPCS
    RELEASE_ID        STRING        NOT NULL,     -- e.g. FY2027, 2026-UPDATE, NLM-2026-11-19
    SOURCE            STRING        NOT NULL,
    SOURCE_URL        STRING,
    EFFECTIVE_FROM    DATE          NOT NULL,
    CODE_COUNT        NUMBER(10),
    BILLABLE_COUNT    NUMBER(10),
    ADDED_COUNT       NUMBER(10),
    CHANGED_COUNT     NUMBER(10),
    RETIRED_COUNT     NUMBER(10),
    CHECKSUM          STRING        NOT NULL,
    LOADED_BY         STRING,
    LOADED_AT         TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_CODE_SET_RELEASES PRIMARY KEY (CODE_SYSTEM, RELEASE_ID)
)
COMMENT = 'Every official code-set release that has been loaded, with change statistics.';

CREATE TABLE IF NOT EXISTS REFERENCE.CODES (
    CODE_SYSTEM         STRING      NOT NULL,
    CODE                STRING      NOT NULL,     -- display form (ICD with dot: I1A.0)
    CODE_RAW            STRING      NOT NULL,     -- no punctuation, upper case (I1A0)
    SHORT_DESCRIPTION   STRING,
    LONG_DESCRIPTION    STRING,
    IS_BILLABLE         BOOLEAN     NOT NULL,     -- ICD: valid for HIPAA claims (leaf code)
    VALID_FROM          DATE        NOT NULL,
    VALID_TO            DATE,                     -- NULL = still in force
    IS_LATEST           BOOLEAN     NOT NULL DEFAULT TRUE,  -- latest known version of this code
    RELEASE_ID          STRING      NOT NULL,     -- release that introduced this version
    RETIRED_IN_RELEASE  STRING,
    ROW_HASH            STRING      NOT NULL,
    LOADED_AT           TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_REFERENCE_CODES PRIMARY KEY (CODE_SYSTEM, CODE_RAW, VALID_FROM)
)
CLUSTER BY (CODE_SYSTEM, CODE_RAW)
COMMENT = 'Code versions with validity periods (type-2 history).';

CREATE TABLE IF NOT EXISTS REFERENCE.CODES_STAGE (
    RUN_ID              STRING      NOT NULL,
    CODE_SYSTEM         STRING      NOT NULL,
    CODE                STRING      NOT NULL,
    CODE_RAW            STRING      NOT NULL,
    SHORT_DESCRIPTION   STRING,
    LONG_DESCRIPTION    STRING,
    IS_BILLABLE         BOOLEAN     NOT NULL,
    SOURCE_VALID_FROM   DATE,                     -- when the source states it (HCPCS add date)
    SOURCE_VALID_TO     DATE,                     -- when the source states it (HCPCS termination)
    ROW_HASH            STRING      NOT NULL
)
COMMENT = 'Transient load area for a sync run (rows are deleted after each run).';

CREATE TABLE IF NOT EXISTS REFERENCE.SYNC_RUNS (
    RUN_ID          STRING        NOT NULL,
    CODE_SYSTEM     STRING        NOT NULL,
    TRIGGERED_BY    STRING,                       -- cli | scheduler | api
    STATUS          STRING        NOT NULL,       -- RUNNING | SUCCEEDED | UP_TO_DATE | FAILED
    RELEASE_ID      STRING,
    MESSAGE         STRING,
    DETAIL          VARIANT,
    STARTED_AT      TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    FINISHED_AT     TIMESTAMP_TZ,
    CONSTRAINT PK_SYNC_RUNS PRIMARY KEY (RUN_ID)
)
COMMENT = 'Audit log of every code-set synchronisation attempt.';

CREATE OR REPLACE VIEW REFERENCE.V_CURRENT_CODES
COMMENT = 'Codes in force today'
AS
SELECT CODE_SYSTEM, CODE, CODE_RAW, SHORT_DESCRIPTION, LONG_DESCRIPTION, IS_BILLABLE, VALID_FROM, VALID_TO, RELEASE_ID
  FROM REFERENCE.CODES
 WHERE VALID_FROM <= CURRENT_DATE()
   AND (VALID_TO IS NULL OR VALID_TO >= CURRENT_DATE());

CREATE OR REPLACE VIEW REFERENCE.V_CODE_SET_STATUS
COMMENT = 'One row per code system: active release, counts, last sync'
AS
SELECT r.CODE_SYSTEM,
       r.RELEASE_ID          AS ACTIVE_RELEASE,
       r.SOURCE,
       r.EFFECTIVE_FROM,
       r.LOADED_AT,
       (SELECT COUNT(*) FROM REFERENCE.V_CURRENT_CODES c WHERE c.CODE_SYSTEM = r.CODE_SYSTEM)                     AS CODES_IN_FORCE,
       (SELECT COUNT(*) FROM REFERENCE.V_CURRENT_CODES c WHERE c.CODE_SYSTEM = r.CODE_SYSTEM AND c.IS_BILLABLE)   AS BILLABLE_IN_FORCE,
       (SELECT MAX(s.FINISHED_AT) FROM REFERENCE.SYNC_RUNS s
         WHERE s.CODE_SYSTEM = r.CODE_SYSTEM AND s.STATUS IN ('SUCCEEDED', 'UP_TO_DATE'))                          AS LAST_CHECKED_AT
  FROM REFERENCE.CODE_SET_RELEASES r
QUALIFY ROW_NUMBER() OVER (PARTITION BY r.CODE_SYSTEM ORDER BY r.EFFECTIVE_FROM DESC, r.LOADED_AT DESC) = 1;

CREATE OR REPLACE VIEW REFERENCE.V_CODE_CHANGES
COMMENT = 'Codes added, revised or deleted by each release'
AS
WITH baseline AS (   -- the first release loaded per system is the baseline, not a change
    SELECT CODE_SYSTEM, MIN_BY(RELEASE_ID, EFFECTIVE_FROM) AS RELEASE_ID
      FROM REFERENCE.CODE_SET_RELEASES GROUP BY CODE_SYSTEM
)
SELECT c.CODE_SYSTEM, c.RELEASE_ID, 'ADDED_OR_REVISED' AS CHANGE, c.CODE, c.LONG_DESCRIPTION, c.VALID_FROM AS CHANGE_DATE
  FROM REFERENCE.CODES c
  LEFT JOIN baseline b ON b.CODE_SYSTEM = c.CODE_SYSTEM AND b.RELEASE_ID = c.RELEASE_ID
 WHERE b.RELEASE_ID IS NULL
UNION ALL
SELECT CODE_SYSTEM, RETIRED_IN_RELEASE, 'DELETED', CODE, LONG_DESCRIPTION, DATEADD(day, 1, VALID_TO)
  FROM REFERENCE.CODES
 WHERE RETIRED_IN_RELEASE IS NOT NULL;
