
-- PolicyOps Recovery Lab
-- Database schema for daily policy snapshot ingestion.
--
-- Each ingestion attempt has its own run ID.
-- Policy changes and audit records will later be written
-- together in a single database transaction.

BEGIN;


-- 1. Record every ingestion attempt, including failures.

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id UUID PRIMARY KEY,

    snapshot_date DATE,
    source_file TEXT,
    checksum_sha256 TEXT,
    schema_version TEXT,
    complete_snapshot BOOLEAN,

    expected_row_count INTEGER CHECK (expected_row_count >= 0),
    source_count INTEGER CHECK (source_count >= 0),
    accepted_count INTEGER CHECK (accepted_count >= 0),

    status TEXT NOT NULL DEFAULT 'registered'
        CHECK (
            status IN (
                'registered',
                'validated',
                'awaiting_review',
                'committed',
                'failed',
                'rejected',
                'replayed'
            )
        ),

    new_count INTEGER CHECK (new_count >= 0),
    changed_count INTEGER CHECK (changed_count >= 0),
    deleted_count INTEGER CHECK (deleted_count >= 0),
    unchanged_count INTEGER CHECK (unchanged_count >= 0),

    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,

    failure_reason TEXT,

    approved_by TEXT,
    approval_reason TEXT,
    approved_at TIMESTAMPTZ,

    CONSTRAINT valid_checksum CHECK (
        checksum_sha256 IS NULL
        OR checksum_sha256 ~ '^[0-9a-f]{64}$'
    ),

    CONSTRAINT committed_run_has_source_identity CHECK (
        status <> 'committed'
        OR (
            snapshot_date IS NOT NULL
            AND checksum_sha256 IS NOT NULL
            AND complete_snapshot IS TRUE
        )
    ),

    CONSTRAINT approval_requires_reason CHECK (
        approved_at IS NULL
        OR (
            NULLIF(BTRIM(approved_by), '') IS NOT NULL
            AND NULLIF(BTRIM(approval_reason), '') IS NOT NULL
        )
    )
);


-- Two successful promotions cannot represent the same date.
-- Failed attempts and retries are still allowed.

CREATE UNIQUE INDEX IF NOT EXISTS
    idx_one_committed_snapshot_per_date
ON ingestion_runs (snapshot_date)
WHERE status = 'committed';


CREATE INDEX IF NOT EXISTS idx_ingestion_runs_date
ON ingestion_runs (snapshot_date DESC, started_at DESC);


-- 2. Store validated and normalized incoming records.
-- The composite primary key prevents duplicate policy IDs
-- within one ingestion run.

CREATE TABLE IF NOT EXISTS policy_staging (
    run_id UUID NOT NULL REFERENCES ingestion_runs(run_id),

    policy_id TEXT NOT NULL
        CHECK (LENGTH(BTRIM(policy_id)) > 0),

    customer_id TEXT NOT NULL
        CHECK (LENGTH(BTRIM(customer_id)) > 0),

    status TEXT NOT NULL
        CHECK (status IN ('active', 'cancelled', 'expired')),

    premium_amount NUMERIC(12, 2) NOT NULL
        CHECK (premium_amount >= 0),

    effective_date DATE NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (run_id, policy_id)
);


CREATE INDEX IF NOT EXISTS idx_staging_policy_id
ON policy_staging (policy_id);


-- 3. Keep the latest known state of each policy.
-- Deleted policies remain in the table with is_deleted = TRUE.

CREATE TABLE IF NOT EXISTS policy_current (
    policy_id TEXT PRIMARY KEY,

    customer_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('active', 'cancelled', 'expired')),

    premium_amount NUMERIC(12, 2) NOT NULL
        CHECK (premium_amount >= 0),

    effective_date DATE NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,

    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,

    latest_run_id UUID NOT NULL
        REFERENCES ingestion_runs(run_id),

    last_seen_snapshot_date DATE NOT NULL
);


-- 4. Record each committed business change.
-- JSONB preserves the before/after evidence without
-- requiring a separate history column for every attribute.

CREATE TABLE IF NOT EXISTS policy_history (
    history_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    run_id UUID NOT NULL
        REFERENCES ingestion_runs(run_id),

    policy_id TEXT NOT NULL,

    change_type TEXT NOT NULL
        CHECK (change_type IN ('insert', 'update', 'delete')),

    old_values JSONB,
    new_values JSONB,

    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT history_has_evidence CHECK (
        old_values IS NOT NULL
        OR new_values IS NOT NULL
    ),

    CONSTRAINT unique_history_per_run_policy
        UNIQUE (run_id, policy_id)
);


CREATE INDEX IF NOT EXISTS idx_history_policy
ON policy_history (policy_id, history_id DESC);


-- 5. Store validation failures and review evidence.
-- These events are linked to ingestion attempts, including
-- attempts that never reached the promotion stage.

CREATE TABLE IF NOT EXISTS quality_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    run_id UUID NOT NULL
        REFERENCES ingestion_runs(run_id),

    check_id TEXT NOT NULL
        CHECK (LENGTH(BTRIM(check_id)) > 0),

    severity TEXT NOT NULL
        CHECK (severity IN ('info', 'warning', 'error')),

    observed_value TEXT,
    expected_rule TEXT NOT NULL,
    evidence JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE INDEX IF NOT EXISTS idx_quality_events_run
ON quality_events (run_id, event_id);


COMMIT;
