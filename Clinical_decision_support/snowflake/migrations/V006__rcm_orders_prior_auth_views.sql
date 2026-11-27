-- =====================================================================
-- V006: typed views over the diagnostic-order, prior-authorization and
-- revenue-cycle sheets the app writes to EHR.SHEETS.
--
-- The app itself does not need these views (it reads the sheets through
-- the API); they let analysts and Snowsight query orders, authorizations,
-- benefits and patient A/R as ordinary tables. Views only: no data moves.
-- Days past due and the aging bucket are recomputed live from CURRENT_DATE.
-- =====================================================================

CREATE OR REPLACE VIEW EHR.V_DIAGNOSTIC_ORDERS
COMMENT = 'Diagnostic orders (labs, imaging, cardiology) linked to patient, MRN and encounter'
AS
SELECT ROW_DATA:"Order_ID"::STRING                         AS ORDER_ID,
       PATIENT_ID,
       ROW_DATA:"MRN"::STRING                              AS MRN,
       ROW_DATA:"Encounter_ID"::STRING                     AS ENCOUNTER_ID,
       TRY_TO_DATE(LEFT(ROW_DATA:"Order_Date"::STRING, 10)) AS ORDER_DATE,
       ROW_DATA:"Order_Type"::STRING                       AS ORDER_TYPE,
       ROW_DATA:"Order_Description"::STRING                AS ORDER_DESCRIPTION,
       ROW_DATA:"CPT_HCPCS"::STRING                        AS CPT_HCPCS,
       ROW_DATA:"LOINC_Code"::STRING                       AS LOINC_CODE,
       ROW_DATA:"Status"::STRING                           AS STATUS,
       ROW_DATA:"Priority"::STRING                         AS PRIORITY,
       ROW_DATA:"Linked_Diagnosis"::STRING                 AS LINKED_DIAGNOSIS,
       ROW_DATA:"Prior_Auth_Status"::STRING                AS PRIOR_AUTH_STATUS,
       ROW_DATA:"Auth_Number"::STRING                      AS AUTH_NUMBER,
       TRY_TO_DATE(LEFT(ROW_DATA:"Performed_Date"::STRING, 10)) AS PERFORMED_DATE,
       ROW_DATA:"Result_Value"::STRING                     AS RESULT_VALUE,
       ROW_DATA:"Result_Flag"::STRING                      AS RESULT_FLAG,
       ROW_DATA:"Ordering_Provider"::STRING                AS ORDERING_PROVIDER,
       ROW_DATA                                            AS ROW_DATA
  FROM EHR.V_SHEET_ROWS
 WHERE SHEET_NAME = 'Orders_Procedures';

CREATE OR REPLACE VIEW EHR.V_PRIOR_AUTHORIZATIONS
COMMENT = 'Payer prior-authorization requests and determinations (ePA / X12 278)'
AS
SELECT ROW_DATA:"PA_ID"::STRING                            AS PA_ID,
       PATIENT_ID,
       ROW_DATA:"MRN"::STRING                              AS MRN,
       ROW_DATA:"Encounter_ID"::STRING                     AS ENCOUNTER_ID,
       ROW_DATA:"Order_ID"::STRING                         AS ORDER_ID,
       ROW_DATA:"CPT_HCPCS"::STRING                        AS CPT_HCPCS,
       ROW_DATA:"Service"::STRING                          AS SERVICE,
       ROW_DATA:"Payer_Name"::STRING                       AS PAYER_NAME,
       ROW_DATA:"Member_ID"::STRING                        AS MEMBER_ID,
       ROW_DATA:"Diagnosis_Codes"::STRING                  AS DIAGNOSIS_CODES,
       ROW_DATA:"Status"::STRING                           AS STATUS,
       ROW_DATA:"Auth_Number"::STRING                      AS AUTH_NUMBER,
       TRY_TO_DATE(LEFT(ROW_DATA:"Valid_From"::STRING, 10))    AS VALID_FROM,
       TRY_TO_DATE(LEFT(ROW_DATA:"Valid_Through"::STRING, 10)) AS VALID_THROUGH,
       TRY_TO_TIMESTAMP_NTZ(ROW_DATA:"Submitted_At"::STRING) AS SUBMITTED_AT,
       TRY_TO_TIMESTAMP_NTZ(ROW_DATA:"Decision_At"::STRING)  AS DECISION_AT,
       ROW_DATA:"Determination_Reason"::STRING             AS DETERMINATION_REASON,
       ROW_DATA                                            AS ROW_DATA
  FROM EHR.V_SHEET_ROWS
 WHERE SHEET_NAME = 'Prior_Authorizations';

CREATE OR REPLACE VIEW BILLING.V_PAYER_PLAN_BENEFITS
COMMENT = 'Eligibility and benefits snapshot per encounter (270/271), with accumulators after adjudication'
AS
SELECT ROW_DATA:"Benefit_Key"::STRING                      AS BENEFIT_KEY,
       PATIENT_ID,
       ROW_DATA:"MRN"::STRING                              AS MRN,
       ROW_DATA:"Encounter_ID"::STRING                     AS ENCOUNTER_ID,
       ROW_DATA:"Payer"::STRING                            AS PAYER,
       ROW_DATA:"Plan_Type"::STRING                        AS PLAN_TYPE,
       ROW_DATA:"Payer_Class"::STRING                      AS PAYER_CLASS,
       ROW_DATA:"Member_ID"::STRING                        AS MEMBER_ID,
       ROW_DATA:"Eligibility_Status"::STRING               AS ELIGIBILITY_STATUS,
       TRY_TO_DATE(LEFT(ROW_DATA:"Eligibility_Check_Date"::STRING, 10)) AS ELIGIBILITY_CHECK_DATE,
       TRY_TO_DOUBLE(ROW_DATA:"Copay"::STRING)             AS COPAY,
       TRY_TO_DOUBLE(ROW_DATA:"Annual_Deductible"::STRING) AS ANNUAL_DEDUCTIBLE,
       TRY_TO_DOUBLE(ROW_DATA:"Deductible_Met_YTD"::STRING) AS DEDUCTIBLE_MET_YTD,
       TRY_TO_DOUBLE(ROW_DATA:"Coinsurance_Pct"::STRING)   AS COINSURANCE_PCT,
       TRY_TO_DOUBLE(ROW_DATA:"OOP_Max"::STRING)           AS OOP_MAX,
       TRY_TO_DOUBLE(ROW_DATA:"OOP_Met_YTD"::STRING)       AS OOP_MET_YTD,
       TRY_TO_DOUBLE(ROW_DATA:"Allowed_Rate"::STRING)      AS ALLOWED_RATE,
       ROW_DATA:"Claim_Status"::STRING                     AS CLAIM_STATUS,
       ROW_DATA:"Guarantor"::STRING                        AS GUARANTOR,
       ROW_DATA:"Guarantor_Relationship"::STRING           AS GUARANTOR_RELATIONSHIP,
       TRY_TO_DATE(LEFT(ROW_DATA:"Insurance_Adjudication_Date"::STRING, 10)) AS INSURANCE_ADJUDICATION_DATE,
       TRY_TO_DATE(LEFT(ROW_DATA:"Statement_Date"::STRING, 10))   AS STATEMENT_DATE,
       TRY_TO_DATE(LEFT(ROW_DATA:"Payment_Due_Date"::STRING, 10)) AS PAYMENT_DUE_DATE,
       TRY_TO_DOUBLE(ROW_DATA:"Deductible_Met_After"::STRING) AS DEDUCTIBLE_MET_AFTER,
       TRY_TO_DOUBLE(ROW_DATA:"OOP_Met_After"::STRING)     AS OOP_MET_AFTER,
       ROW_DATA                                            AS ROW_DATA
  FROM EHR.V_SHEET_ROWS
 WHERE SHEET_NAME = 'Payer_Plan_Benefits';

CREATE OR REPLACE VIEW BILLING.V_PATIENT_BALANCE
COMMENT = 'Patient accounts: charges, allowed, cost share, payments, live aging and next action'
AS
WITH accounts AS (
    SELECT ROW_DATA:"Account_ID"::STRING                   AS ACCOUNT_ID,
           PATIENT_ID,
           ROW_DATA:"MRN"::STRING                          AS MRN,
           ROW_DATA:"Encounter_ID"::STRING                 AS ENCOUNTER_ID,
           TRY_TO_DATE(LEFT(ROW_DATA:"Service_Date"::STRING, 10)) AS SERVICE_DATE,
           ROW_DATA:"Payer"::STRING                        AS PAYER,
           ROW_DATA:"Claim_IDs"::STRING                    AS CLAIM_IDS,
           TRY_TO_DOUBLE(ROW_DATA:"Charge"::STRING)        AS CHARGE,
           TRY_TO_DOUBLE(ROW_DATA:"Allowed_Amount"::STRING) AS ALLOWED_AMOUNT,
           TRY_TO_DOUBLE(ROW_DATA:"Contractual_Adjustment"::STRING) AS CONTRACTUAL_ADJUSTMENT,
           TRY_TO_DOUBLE(ROW_DATA:"Denied_Amount"::STRING) AS DENIED_AMOUNT,
           TRY_TO_DOUBLE(ROW_DATA:"Copay_Applied"::STRING) AS COPAY_APPLIED,
           TRY_TO_DOUBLE(ROW_DATA:"Deductible_Applied"::STRING) AS DEDUCTIBLE_APPLIED,
           TRY_TO_DOUBLE(ROW_DATA:"Coinsurance_Applied"::STRING) AS COINSURANCE_APPLIED,
           TRY_TO_DOUBLE(ROW_DATA:"Patient_Responsibility"::STRING) AS PATIENT_RESPONSIBILITY,
           TRY_TO_DOUBLE(ROW_DATA:"Payer_Expected_Payment"::STRING) AS PAYER_EXPECTED_PAYMENT,
           TRY_TO_DOUBLE(ROW_DATA:"Payer_Paid"::STRING)    AS PAYER_PAID,
           COALESCE(TRY_TO_DOUBLE(ROW_DATA:"Patient_Paid_At_Discharge"::STRING), 0) AS PATIENT_PAID_AT_DISCHARGE,
           COALESCE(TRY_TO_DOUBLE(ROW_DATA:"Patient_Payments"::STRING), 0)          AS PATIENT_PAYMENTS,
           COALESCE(TRY_TO_DOUBLE(ROW_DATA:"Financial_Assistance"::STRING), 0)      AS FINANCIAL_ASSISTANCE,
           ROW_DATA:"Claim_Status"::STRING                 AS CLAIM_STATUS,
           ROW_DATA:"Denial_Reasons"::STRING               AS DENIAL_REASONS,
           TRY_TO_DATE(LEFT(ROW_DATA:"Insurance_Adjudication_Date"::STRING, 10)) AS INSURANCE_ADJUDICATION_DATE,
           TRY_TO_DATE(LEFT(ROW_DATA:"Statement_Date"::STRING, 10))   AS STATEMENT_DATE,
           TRY_TO_DATE(LEFT(ROW_DATA:"Payment_Due_Date"::STRING, 10)) AS PAYMENT_DUE_DATE,
           ROW_DATA:"Follow_up_Priority"::STRING           AS FOLLOW_UP_PRIORITY_AT_SAVE,
           ROW_DATA:"Recommended_Action"::STRING           AS RECOMMENDED_ACTION_AT_SAVE,
           ROW_DATA:"Guarantor"::STRING                    AS GUARANTOR
      FROM EHR.V_SHEET_ROWS
     WHERE SHEET_NAME = 'Patient_Balance'
)
SELECT a.*,
       ROUND(COALESCE(a.PATIENT_RESPONSIBILITY, 0) - a.PATIENT_PAID_AT_DISCHARGE - a.PATIENT_PAYMENTS - a.FINANCIAL_ASSISTANCE, 2)
                                                            AS PATIENT_BALANCE,
       IFF(PATIENT_BALANCE > 0 AND a.PAYMENT_DUE_DATE IS NOT NULL,
           GREATEST(0, DATEDIFF('day', a.PAYMENT_DUE_DATE, CURRENT_DATE())), 0) AS DAYS_PAST_DUE,
       CASE
           WHEN a.CLAIM_STATUS IN ('Ready to submit', 'Draft') THEN 'Not yet billed'
           WHEN a.CLAIM_STATUS NOT IN ('Paid', 'Partially denied', 'Denied') THEN 'With payer'
           WHEN PATIENT_BALANCE < 0 THEN 'Credit balance'
           WHEN PATIENT_BALANCE = 0 THEN 'No balance'
           WHEN a.PAYMENT_DUE_DATE IS NULL THEN 'Statement pending'
           WHEN DAYS_PAST_DUE = 0 THEN 'Current'
           WHEN DAYS_PAST_DUE <= 30 THEN '1-30'
           WHEN DAYS_PAST_DUE <= 60 THEN '31-60'
           WHEN DAYS_PAST_DUE <= 90 THEN '61-90'
           ELSE '90+'
       END                                                  AS AGING_BUCKET
  FROM accounts a;

CREATE OR REPLACE VIEW BILLING.V_PATIENT_AR_AGING
COMMENT = 'Open patient balances by live aging bucket'
AS
SELECT AGING_BUCKET,
       COUNT(*)                         AS ACCOUNTS,
       ROUND(SUM(PATIENT_BALANCE), 2)   AS PATIENT_BALANCE
  FROM BILLING.V_PATIENT_BALANCE
 WHERE PATIENT_BALANCE > 0
 GROUP BY AGING_BUCKET;
