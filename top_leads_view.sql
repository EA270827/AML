CREATE OR ALTER VIEW dbo.aml_top_leads AS
WITH latest AS (
    SELECT MAX(run_date) AS run_date FROM dbo.aml_screening_results
),
current_rows AS (
    SELECT r.*
    FROM dbo.aml_screening_results r
    CROSS JOIN latest l
    WHERE r.run_date = l.run_date
      AND r.model_version = (
            SELECT MAX(model_version)
            FROM dbo.aml_screening_results
            WHERE run_date = l.run_date
          )
),
ranked AS (
    SELECT
        match_acid, match_name, match_foracid, cif_id,
        account_category, account_status, seed_overlap,
        priority_band, distance, risk_reason, risk_reason_technical,
        total_credit, total_debit, turnover, account_age_days, run_date,
        ROW_NUMBER() OVER (PARTITION BY match_acid ORDER BY distance ASC) AS rn
    FROM current_rows
)
SELECT
    match_acid, match_name, match_foracid, cif_id,
    account_category, account_status, seed_overlap,
    priority_band AS best_band, distance AS best_distance,
    risk_reason, risk_reason_technical,
    total_credit, total_debit, turnover, account_age_days, run_date
FROM ranked
WHERE rn = 1;