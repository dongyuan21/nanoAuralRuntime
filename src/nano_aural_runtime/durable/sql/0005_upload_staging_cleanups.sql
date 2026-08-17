-- Phase 3E bounded staging-object cleanup evidence.  A separate ledger avoids
-- mutating terminal upload sessions, whose identity and state are immutable.

CREATE TABLE upload_staging_cleanups (
    upload_session_id UUID PRIMARY KEY REFERENCES upload_sessions(id) ON DELETE RESTRICT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
