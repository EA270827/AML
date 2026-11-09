/* ============================================================
   AML Watchlist Screening — reporting DB schema (SQL Server)
   Run once against the reporting database (NOT the Finacle replica).
   ============================================================ */

/* 1. Watchlist of known reference accounts to screen against ---------- */
IF OBJECT_ID('dbo.aml_watchlist', 'U') IS NULL
CREATE TABLE dbo.aml_watchlist (
    acid        VARCHAR(20)   NOT NULL PRIMARY KEY,
    acct_name   NVARCHAR(200) NULL,
    added_date  DATE          NOT NULL DEFAULT CAST(SYSUTCDATETIME() AS DATE),
    active      BIT           NOT NULL DEFAULT 1,
    notes       NVARCHAR(500) NULL
);
-- Seed the first reference account (Frontier):
-- INSERT INTO dbo.aml_watchlist (acid, acct_name, notes)
-- VALUES ('UG6246002', 'FRONTIER TEXTILE DEALERS CO LIMITED', 'Pilot reference account');

/* 1b. Exclusion list — known-legitimate entities suppressed from results --- */
/*     (v2.8) A row may specify a CIF (whole customer) OR an ACID (one account).*/
/*     Excluded accounts stay in the population for distance/context; they are  */
/*     only removed from surfaced leads. Kept as an auditable record of who     */
/*     excluded whom and why.                                                   */
IF OBJECT_ID('dbo.aml_exclusions', 'U') IS NULL
CREATE TABLE dbo.aml_exclusions (
    exclusion_id INT IDENTITY(1,1) PRIMARY KEY,
    cif_id       VARCHAR(20)   NULL,   -- exclude the whole customer, OR
    acid         VARCHAR(20)   NULL,   -- exclude one specific account
    entity_name  NVARCHAR(200) NULL,
    reason       NVARCHAR(300) NOT NULL,   -- e.g. 'payment aggregator', 'distributor'
    added_by     NVARCHAR(120) NULL,
    added_date   DATE          NOT NULL DEFAULT CAST(SYSUTCDATETIME() AS DATE),
    active       BIT           NOT NULL DEFAULT 1,
    CONSTRAINT ck_excl_one_key CHECK (cif_id IS NOT NULL OR acid IS NOT NULL)
);
-- Example: exclude a payment aggregator by CIF
-- INSERT INTO dbo.aml_exclusions (cif_id, entity_name, reason, added_by)
-- VALUES ('2000123456', 'INTERSWITCH', 'payment aggregator - legitimate high flow-through', 'M.Analyst');

/* 2. Frozen model parameters, versioned ------------------------------- */
IF OBJECT_ID('dbo.aml_config', 'U') IS NULL
CREATE TABLE dbo.aml_config (
    model_version VARCHAR(20)   NOT NULL,
    param_name    VARCHAR(60)   NOT NULL,
    param_value   NVARCHAR(100) NOT NULL,   -- JSON-encoded scalar
    created_at    DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT pk_aml_config PRIMARY KEY (model_version, param_name)
);

/* 3. Daily results snapshot (the Power BI fact table) ----------------- */
IF OBJECT_ID('dbo.aml_screening_results', 'U') IS NULL
CREATE TABLE dbo.aml_screening_results (
    run_id                     UNIQUEIDENTIFIER NOT NULL,
    run_date                   DATE             NOT NULL,
    model_version              VARCHAR(20)      NOT NULL,
    reference_acid             VARCHAR(20)      NOT NULL,
    reference_name             NVARCHAR(200)    NULL,
    match_acid                 VARCHAR(20)      NOT NULL,
    match_foracid              VARCHAR(30)      NULL,
    match_name                 NVARCHAR(200)    NULL,
    sol_id                     VARCHAR(20)      NULL,
    cif_id                     VARCHAR(20)      NULL,   -- v2.3: customer id
    account_category           VARCHAR(10)      NULL,   -- v2.3: Business / Personal / Unknown
    sector                     NVARCHAR(60)     NULL,   -- v2.7: raw sector from RM map
    sub_unit                   VARCHAR(15)      NULL,   -- v2.7: Enterprise / Commercial / Unclassified
    rm_name                    NVARCHAR(120)    NULL,   -- v2.7
    rm_email                   NVARCHAR(120)    NULL,   -- v2.7
    segment_manager            NVARCHAR(120)    NULL,   -- v2.7
    has_credit_relationship    BIT              NULL,   -- v2.10: active loan (book_bal_lcy_eq < 0)
    schm_type                  VARCHAR(20)      NULL,
    account_status             VARCHAR(10)      NULL,   -- v2.0: Open / Closed
    distance                   FLOAT            NOT NULL,
    rank_within_ref            INT              NOT NULL,
    seed_overlap               INT              NULL,   -- v2.2: # reference seeds this account matches
    priority_band              VARCHAR(10)      NOT NULL,
    risk_reason                NVARCHAR(1000)   NULL,
    risk_reason_technical      NVARCHAR(1500)   NULL,   -- v2.4: detailed register for analyst/audit
    flow_through_ratio         FLOAT            NULL,
    retention_pct              FLOAT            NULL,
    largest_credit_share       FLOAT            NULL,
    largest_debit_share        FLOAT            NULL,
    top3_credit_share          FLOAT            NULL,
    top3_debit_share           FLOAT            NULL,
    median_hours_to_next_debit FLOAT            NULL,
    frac_credits_followed_24h  FLOAT            NULL,
    account_age_days           INT              NULL,
    total_credit               FLOAT            NULL,   -- v2.5
    total_debit                FLOAT            NULL,   -- v2.5
    turnover                   FLOAT            NULL,
    turnover_per_day           FLOAT            NULL,
    loaded_at                  DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()
);
-- Indexes for typical Power BI slicers / trend queries:
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_results_run_date')
    CREATE INDEX ix_results_run_date ON dbo.aml_screening_results (run_date);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_results_ref')
    CREATE INDEX ix_results_ref ON dbo.aml_screening_results (reference_acid, run_date);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_results_match')
    CREATE INDEX ix_results_match ON dbo.aml_screening_results (match_acid, run_date);

/* 4. Run audit log ---------------------------------------------------- */
IF OBJECT_ID('dbo.aml_run_log', 'U') IS NULL
CREATE TABLE dbo.aml_run_log (
    run_id               UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    run_date             DATE             NOT NULL,
    model_version        VARCHAR(20)      NOT NULL,
    started_at           DATETIME2        NOT NULL,
    finished_at          DATETIME2        NULL,
    status               VARCHAR(10)      NOT NULL,   -- SUCCESS / FAILED
    n_accounts_profiled  INT              NULL,
    n_reference_accounts INT              NULL,
    n_results            INT              NULL,
    params_hash          VARCHAR(32)      NULL,
    error_message        NVARCHAR(1000)   NULL
);

/* 5. Related-account discovery (v2.9) --------------------------------- */
/*    For each confirmed-mule reference CIF, the OPEN accounts that customer  */
/*    also holds which are NOT themselves on the flagged list — pure          */
/*    discovery of a known mule's live footprint. Context, not a conclusion.  */
IF OBJECT_ID('dbo.aml_related_discovery', 'U') IS NULL
CREATE TABLE dbo.aml_related_discovery (
    run_id            UNIQUEIDENTIFIER NOT NULL,
    run_date          DATE             NOT NULL,
    reference_acid    VARCHAR(20)      NULL,   -- the confirmed mule we expanded from
    reference_name    NVARCHAR(200)    NULL,
    cif_id            VARCHAR(20)      NULL,   -- shared customer
    related_acid      VARCHAR(20)      NULL,   -- discovered open account
    related_foracid   VARCHAR(30)      NULL,
    related_name      NVARCHAR(200)    NULL,
    related_crncy     VARCHAR(5)       NULL,
    related_opn_date  DATE             NULL,
    loaded_at         DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_related_run_date')
    CREATE INDEX ix_related_run_date ON dbo.aml_related_discovery (run_date);

/* 6. Notification staging (v2.11) ------------------------------------- */
/*    Prepared notifications for NEWLY-added flagged accounts. The pipeline    */
/*    STAGES these (status='pending'); a separate, gated sender delivers them. */
/*    Nothing is emailed until the sender is explicitly enabled and compliance */
/*    has reviewed the staged content.                                         */
IF OBJECT_ID('dbo.aml_notifications', 'U') IS NULL
CREATE TABLE dbo.aml_notifications (
    notification_id  INT IDENTITY(1,1) PRIMARY KEY,
    run_id           UNIQUEIDENTIFIER NOT NULL,
    run_date         DATE             NOT NULL,
    recipient_type   VARCHAR(15)      NOT NULL,   -- 'RM' or 'MANAGEMENT'
    recipient_name   NVARCHAR(120)    NULL,
    recipient_email  NVARCHAR(120)    NULL,
    cc_email         NVARCHAR(400)    NULL,   -- v2.13: personnel CC'd (segment mgr / management)
    subject          NVARCHAR(200)    NULL,
    body             NVARCHAR(MAX)    NULL,
    n_accounts       INT              NULL,        -- how many new accounts this covers
    status           VARCHAR(10)      NOT NULL DEFAULT 'pending',  -- pending / sent / skipped
    sent_at          DATETIME2        NULL,
    loaded_at        DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_notif_status')
    CREATE INDEX ix_notif_status ON dbo.aml_notifications (status, run_date);

/* 7. Network discovery (v2.12) --------------------------------------- */
/*    Accounts linked to a confirmed mule through a SHARED PARTY (beneficial   */
/*    owner / shareholder / corp rep / guarantor) rather than shared ownership.*/
/*    Catches controllers that CIF-discovery is blind to. Context, not proof:  */
/*    a shared party is a place to look, not evidence of guilt.                */
IF OBJECT_ID('dbo.aml_network_discovery', 'U') IS NULL
CREATE TABLE dbo.aml_network_discovery (
    run_id            UNIQUEIDENTIFIER NOT NULL,
    run_date          DATE             NOT NULL,
    reference_acid    VARCHAR(20)      NULL,   -- confirmed mule we expanded from
    reference_name    NVARCHAR(200)    NULL,
    mule_cif          VARCHAR(20)      NULL,
    relationship      NVARCHAR(60)     NULL,   -- e.g. 'Beneficial Owner'
    linked_party_cif  VARCHAR(20)      NULL,   -- the shared party
    linked_acid       VARCHAR(20)      NULL,   -- discovered open account of that party
    linked_foracid    VARCHAR(30)      NULL,
    linked_name       NVARCHAR(200)    NULL,
    linked_crncy      VARCHAR(5)       NULL,
    linked_opn_date   DATE             NULL,
    loaded_at         DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_network_run_date')
    CREATE INDEX ix_network_run_date ON dbo.aml_network_discovery (run_date);