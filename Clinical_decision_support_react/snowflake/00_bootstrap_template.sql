-- =====================================================================
-- NuuCare - Snowflake bootstrap TEMPLATE
--
-- Do not run this file directly. Generate your copy (with this machine's public keys) with:
--     .venv/Scripts/python -m app.db.setup init --account <ORGNAME-ACCOUNTNAME>
-- It writes snowflake/local/00_bootstrap_run_once.sql (git-ignored) and copies it to the clipboard.
--
-- Generated copy: run ONCE, as ACCOUNTADMIN
--
-- In VS Code: open this file, then "Execute All Statements".
-- Safe to re-run: every statement is idempotent.
--
-- Creates account-level objects only (warehouse, roles, database, service
-- users). Tables, views and procedures are created by versioned migrations:
--     .venv\Scripts\python -m app.db.migrate
-- =====================================================================
USE ROLE ACCOUNTADMIN;

-- ---------- Cost guardrail: hard monthly credit cap ----------
CREATE RESOURCE MONITOR IF NOT EXISTS NUUCARE_MONITOR
  WITH CREDIT_QUOTA = 25
       FREQUENCY = MONTHLY
       START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 75 PERCENT DO NOTIFY
           ON 90 PERCENT DO SUSPEND
           ON 100 PERCENT DO SUSPEND_IMMEDIATE;

-- ---------- Compute: smallest size, suspends after 60s idle ----------
CREATE WAREHOUSE IF NOT EXISTS NUUCARE_WH
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  STATEMENT_TIMEOUT_IN_SECONDS = 300
  COMMENT = 'NuuCare clinical platform - application workload';
ALTER WAREHOUSE NUUCARE_WH SET RESOURCE_MONITOR = NUUCARE_MONITOR;

-- ---------- Roles (least privilege) ----------
--   NUUCARE_OWNER    owns the database; used only by migrations
--   NUUCARE_APP      read/write data, call procedures; used by the app
--   NUUCARE_READONLY select-only; analysts / reporting
CREATE ROLE IF NOT EXISTS NUUCARE_READONLY COMMENT = 'NuuCare read-only (reporting)';
CREATE ROLE IF NOT EXISTS NUUCARE_APP      COMMENT = 'NuuCare application read/write';
CREATE ROLE IF NOT EXISTS NUUCARE_OWNER    COMMENT = 'NuuCare schema owner (migrations only)';
GRANT ROLE NUUCARE_READONLY TO ROLE NUUCARE_APP;
GRANT ROLE NUUCARE_APP      TO ROLE NUUCARE_OWNER;
GRANT ROLE NUUCARE_OWNER    TO ROLE SYSADMIN;

-- ---------- Database ----------
CREATE DATABASE IF NOT EXISTS NUUCARE_DB
  DATA_RETENTION_TIME_IN_DAYS = 1
  COMMENT = 'NuuCare clinical platform (Ambient Scribe, CDS, Coding, Billing, VBC)';
GRANT OWNERSHIP ON DATABASE NUUCARE_DB TO ROLE NUUCARE_OWNER COPY CURRENT GRANTS;
GRANT USAGE ON WAREHOUSE NUUCARE_WH TO ROLE NUUCARE_READONLY;
GRANT USAGE ON DATABASE NUUCARE_DB  TO ROLE NUUCARE_READONLY;

-- Future grants: every schema/table/view/procedure the migrations create is
-- automatically readable (READONLY) and writable (APP).
GRANT USAGE  ON FUTURE SCHEMAS    IN DATABASE NUUCARE_DB TO ROLE NUUCARE_READONLY;
GRANT SELECT ON FUTURE TABLES     IN DATABASE NUUCARE_DB TO ROLE NUUCARE_READONLY;
GRANT SELECT ON FUTURE VIEWS      IN DATABASE NUUCARE_DB TO ROLE NUUCARE_READONLY;
GRANT INSERT, UPDATE, DELETE ON FUTURE TABLES IN DATABASE NUUCARE_DB TO ROLE NUUCARE_APP;
GRANT USAGE  ON FUTURE PROCEDURES IN DATABASE NUUCARE_DB TO ROLE NUUCARE_APP;
GRANT USAGE  ON FUTURE FUNCTIONS  IN DATABASE NUUCARE_DB TO ROLE NUUCARE_APP;

-- ---------- Service users: key-pair auth only, no password, no MFA prompts ----------
-- Private keys live in %USERPROFILE%\.snowflake\keys (never in the project folder).
CREATE USER IF NOT EXISTS NUUCARE_APP_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = NUUCARE_APP
  DEFAULT_WAREHOUSE = NUUCARE_WH
  DEFAULT_NAMESPACE = NUUCARE_DB
  COMMENT = 'NuuCare Streamlit + FastAPI runtime';
ALTER USER NUUCARE_APP_SVC SET RSA_PUBLIC_KEY = '{{APP_PUBLIC_KEY}}';
GRANT ROLE NUUCARE_APP TO USER NUUCARE_APP_SVC;

CREATE USER IF NOT EXISTS NUUCARE_DEPLOY_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = NUUCARE_OWNER
  DEFAULT_WAREHOUSE = NUUCARE_WH
  DEFAULT_NAMESPACE = NUUCARE_DB
  COMMENT = 'NuuCare schema migrations';
ALTER USER NUUCARE_DEPLOY_SVC SET RSA_PUBLIC_KEY = '{{DEPLOY_PUBLIC_KEY}}';
GRANT ROLE NUUCARE_OWNER TO USER NUUCARE_DEPLOY_SVC;

-- ---------- Verify ----------
SHOW USERS LIKE 'NUUCARE%';
SELECT 'Bootstrap complete - tell Claude to continue' AS STATUS;
