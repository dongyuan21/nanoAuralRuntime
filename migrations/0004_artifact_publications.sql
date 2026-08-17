-- Phase 3E artifact-publication authority. PostgreSQL is the only authority
-- for publication progress and visible winners. This file is byte-identical
-- to the packaged durable/sql mirror.

CREATE TYPE publication_state AS ENUM (
    'reserved', 'object_written', 'validated', 'finalized', 'rejected', 'abandoned'
);

CREATE TABLE artifact_publications (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL,
    attempt_id UUID NOT NULL,
    worker_id UUID NOT NULL REFERENCES workers(id),
    lease_epoch BIGINT NOT NULL CHECK (lease_epoch > 0),
    kind TEXT NOT NULL CHECK (kind IN ('output', 'manifest')),
    expected_sha256 CHAR(64) NULL
        CHECK (expected_sha256 IS NULL OR expected_sha256 ~ '^[0-9a-f]{64}$'),
    expected_size_bytes BIGINT NULL CHECK (expected_size_bytes IS NULL OR expected_size_bytes >= 0),
    expected_content_type TEXT NOT NULL CHECK (length(trim(expected_content_type)) > 0),
    max_size_bytes BIGINT NOT NULL CHECK (max_size_bytes > 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt_object_key TEXT NULL,
    observed_sha256 CHAR(64) NULL
        CHECK (observed_sha256 IS NULL OR observed_sha256 ~ '^[0-9a-f]{64}$'),
    observed_size_bytes BIGINT NULL CHECK (observed_size_bytes IS NULL OR observed_size_bytes >= 0),
    observed_content_type TEXT NULL,
    validator_metadata JSONB NULL,
    canonical_blob_id UUID NULL REFERENCES blobs(id),
    state publication_state NOT NULL DEFAULT 'reserved',
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    terminal_reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    terminal_at TIMESTAMPTZ NULL,
    attempt_object_deleted_at TIMESTAMPTZ NULL,
    stale_since TIMESTAMPTZ NULL,
    UNIQUE (attempt_id, kind),
    UNIQUE (job_id, id),
    FOREIGN KEY (job_id, attempt_id) REFERENCES job_attempts(job_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (expected_sha256 IS NULL OR observed_sha256 IS NULL
           OR expected_sha256 = observed_sha256),
    CHECK (expected_size_bytes IS NULL OR observed_size_bytes IS NULL
           OR expected_size_bytes = observed_size_bytes),
    CHECK (expected_size_bytes IS NULL OR expected_size_bytes <= max_size_bytes),
    CHECK (observed_size_bytes IS NULL OR observed_size_bytes <= max_size_bytes),
    CHECK (observed_content_type IS NULL OR observed_content_type = expected_content_type),
    CHECK (attempt_object_deleted_at IS NULL OR state IN ('finalized', 'rejected', 'abandoned')),
    CHECK (
        attempt_object_key IS NULL OR
        attempt_object_key = 'attempts/' || job_id::text || '/' || attempt_id::text ||
            '/epoch-' || lease_epoch::text || '/' || kind || '/' || id::text
    ),
    CHECK (
        (state = 'reserved' AND attempt_object_key IS NULL
         AND observed_sha256 IS NULL AND observed_size_bytes IS NULL
         AND observed_content_type IS NULL AND validator_metadata IS NULL
         AND canonical_blob_id IS NULL AND terminal_at IS NULL AND terminal_reason IS NULL)
        OR
        (state = 'object_written' AND attempt_object_key IS NOT NULL
         AND observed_sha256 IS NOT NULL AND observed_size_bytes IS NOT NULL
         AND observed_content_type IS NULL AND validator_metadata IS NULL
         AND canonical_blob_id IS NULL AND terminal_at IS NULL AND terminal_reason IS NULL)
        OR
        (state = 'validated' AND attempt_object_key IS NOT NULL
         AND observed_sha256 IS NOT NULL AND observed_size_bytes IS NOT NULL
         AND observed_content_type IS NOT NULL AND validator_metadata IS NOT NULL
         AND canonical_blob_id IS NOT NULL AND terminal_at IS NULL AND terminal_reason IS NULL)
        OR
        (state = 'finalized' AND attempt_object_key IS NOT NULL
         AND observed_sha256 IS NOT NULL AND observed_size_bytes IS NOT NULL
         AND observed_content_type IS NOT NULL AND validator_metadata IS NOT NULL
         AND canonical_blob_id IS NOT NULL AND terminal_at IS NOT NULL AND terminal_reason IS NULL
         AND stale_since IS NULL)
        OR
        (state = 'rejected' AND terminal_at IS NOT NULL AND stale_since IS NULL
         AND terminal_reason IS NOT NULL AND length(trim(terminal_reason)) > 0)
        OR
        (state = 'abandoned' AND terminal_at IS NOT NULL AND stale_since IS NOT NULL
         AND terminal_reason IS NOT NULL AND length(trim(terminal_reason)) > 0)
    )
);

-- Phase 3A never had a publication ledger. Refuse an ambiguous in-place
-- upgrade rather than silently grandfathering a visible artifact that cannot
-- be proven to have passed the new fenced publication state machine.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM artifacts) THEN
        RAISE EXCEPTION '0004 requires artifact backfill before upgrade';
    END IF;
END;
$$;

ALTER TABLE artifacts
    ADD COLUMN publication_id UUID NOT NULL UNIQUE
        REFERENCES artifact_publications(id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX publication_cleanup_idx
    ON artifact_publications (created_at, id)
    WHERE state IN ('reserved', 'object_written', 'validated', 'rejected', 'abandoned');
CREATE UNIQUE INDEX one_job_succeeded_event_per_job
    ON job_events (job_id) WHERE event_type = 'job_succeeded';

CREATE FUNCTION ensure_publication_identity_and_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'reserved' OR NEW.version <> 0 THEN
            RAISE EXCEPTION 'publication must begin RESERVED at version zero';
        END IF;
        NEW.created_at := clock_timestamp();
        NEW.updated_at := NEW.created_at;
        NEW.terminal_at := NULL;
        NEW.attempt_object_deleted_at := NULL;
        NEW.stale_since := NULL;
    ELSE
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.job_id IS DISTINCT FROM OLD.job_id
           OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
           OR NEW.worker_id IS DISTINCT FROM OLD.worker_id
           OR NEW.lease_epoch IS DISTINCT FROM OLD.lease_epoch
           OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.expected_sha256 IS DISTINCT FROM OLD.expected_sha256
           OR NEW.expected_size_bytes IS DISTINCT FROM OLD.expected_size_bytes
           OR NEW.expected_content_type IS DISTINCT FROM OLD.expected_content_type
           OR NEW.max_size_bytes IS DISTINCT FROM OLD.max_size_bytes
           OR NEW.metadata IS DISTINCT FROM OLD.metadata
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'publication identity is immutable';
        END IF;
        IF OLD.state IN ('finalized', 'rejected', 'abandoned') THEN
            IF NEW.state = OLD.state AND NEW.version = OLD.version + 1
               AND OLD.attempt_object_deleted_at IS NULL
               AND NEW.attempt_object_deleted_at IS NOT NULL
               AND NEW.attempt_object_key IS NOT DISTINCT FROM OLD.attempt_object_key
               AND NEW.observed_sha256 IS NOT DISTINCT FROM OLD.observed_sha256
               AND NEW.observed_size_bytes IS NOT DISTINCT FROM OLD.observed_size_bytes
               AND NEW.observed_content_type IS NOT DISTINCT FROM OLD.observed_content_type
               AND NEW.validator_metadata IS NOT DISTINCT FROM OLD.validator_metadata
               AND NEW.canonical_blob_id IS NOT DISTINCT FROM OLD.canonical_blob_id
               AND NEW.terminal_reason IS NOT DISTINCT FROM OLD.terminal_reason
               AND NEW.terminal_at IS NOT DISTINCT FROM OLD.terminal_at
               AND NEW.stale_since IS NOT DISTINCT FROM OLD.stale_since THEN
                NEW.attempt_object_deleted_at := clock_timestamp();
                NEW.updated_at := NEW.attempt_object_deleted_at;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'terminal publication is immutable except cleanup evidence';
        END IF;
        IF NEW.state = OLD.state AND NEW.version = OLD.version + 1
           AND OLD.stale_since IS NULL AND NEW.stale_since IS NOT NULL
           AND NEW.attempt_object_key IS NOT DISTINCT FROM OLD.attempt_object_key
           AND NEW.observed_sha256 IS NOT DISTINCT FROM OLD.observed_sha256
           AND NEW.observed_size_bytes IS NOT DISTINCT FROM OLD.observed_size_bytes
           AND NEW.observed_content_type IS NOT DISTINCT FROM OLD.observed_content_type
           AND NEW.validator_metadata IS NOT DISTINCT FROM OLD.validator_metadata
           AND NEW.canonical_blob_id IS NOT DISTINCT FROM OLD.canonical_blob_id
           AND NEW.terminal_reason IS NOT DISTINCT FROM OLD.terminal_reason
           AND NEW.terminal_at IS NOT DISTINCT FROM OLD.terminal_at
           AND NEW.attempt_object_deleted_at IS NULL THEN
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END IF;
        IF NEW.attempt_object_deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'attempt object cleanup requires an already-terminal publication';
        END IF;
        IF NEW.version <> OLD.version + 1 OR NOT (
            (OLD.state = 'reserved' AND NEW.state IN ('object_written', 'rejected', 'abandoned')) OR
            (OLD.state = 'object_written' AND NEW.state IN ('validated', 'rejected', 'abandoned')) OR
            (OLD.state = 'validated' AND NEW.state IN ('finalized', 'rejected', 'abandoned'))
        ) THEN
            RAISE EXCEPTION 'invalid publication state/version transition';
        END IF;
        IF OLD.attempt_object_key IS NOT NULL
           AND NEW.attempt_object_key IS DISTINCT FROM OLD.attempt_object_key THEN
            RAISE EXCEPTION 'publication object identity is immutable';
        END IF;
        IF OLD.observed_sha256 IS NOT NULL
           AND (NEW.observed_sha256 IS DISTINCT FROM OLD.observed_sha256
                OR NEW.observed_size_bytes IS DISTINCT FROM OLD.observed_size_bytes) THEN
            RAISE EXCEPTION 'publication observed identity is immutable';
        END IF;
        IF OLD.canonical_blob_id IS NOT NULL
           AND NEW.canonical_blob_id IS DISTINCT FROM OLD.canonical_blob_id THEN
            RAISE EXCEPTION 'publication canonical blob is immutable';
        END IF;
        IF OLD.observed_content_type IS NOT NULL
           AND (NEW.observed_content_type IS DISTINCT FROM OLD.observed_content_type
                OR NEW.validator_metadata IS DISTINCT FROM OLD.validator_metadata) THEN
            RAISE EXCEPTION 'publication validation evidence is immutable';
        END IF;
        IF NEW.state IN ('finalized', 'rejected', 'abandoned') THEN
            NEW.terminal_at := clock_timestamp();
        ELSIF NEW.terminal_at IS NOT NULL THEN
            RAISE EXCEPTION 'nonterminal publication cannot have terminal_at';
        END IF;
        IF NEW.state <> 'abandoned' AND NEW.stale_since IS DISTINCT FROM OLD.stale_since THEN
            RAISE EXCEPTION 'stale_since is DB-owned abandonment evidence';
        END IF;
        NEW.updated_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER publication_identity_transition_guard
    BEFORE INSERT OR UPDATE ON artifact_publications
    FOR EACH ROW EXECUTE FUNCTION ensure_publication_identity_and_transition();

CREATE FUNCTION ensure_publication_current_lease() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    lease_is_current BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM workers w
        JOIN jobs j ON j.id = NEW.job_id
        JOIN job_attempts a ON a.id = NEW.attempt_id AND a.job_id = j.id
        WHERE w.id = NEW.worker_id AND w.state = 'busy'
          AND a.worker_id = w.id AND a.lease_epoch = NEW.lease_epoch
          AND a.state = 'active' AND a.lease_expires_at > clock_timestamp()
          AND j.state = 'running' AND j.current_attempt_id = a.id
          AND j.lease_epoch = a.lease_epoch AND j.cancel_requested_at IS NULL
    ) INTO lease_is_current;
    IF TG_OP = 'UPDATE' AND OLD.state IN ('finalized', 'rejected', 'abandoned')
       AND OLD.attempt_object_deleted_at IS NULL
       AND NEW.attempt_object_deleted_at IS NOT NULL THEN
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' AND NEW.state = OLD.state
       AND OLD.stale_since IS NULL AND NEW.stale_since IS NOT NULL THEN
        IF lease_is_current THEN
            RAISE EXCEPTION 'current publication cannot begin stale grace';
        END IF;
        NEW.stale_since := clock_timestamp();
    ELSIF NEW.state = 'abandoned' THEN
        IF lease_is_current OR TG_OP <> 'UPDATE' OR OLD.stale_since IS NULL
           OR clock_timestamp() < OLD.stale_since + interval '5 minutes' THEN
            RAISE EXCEPTION 'ABANDONED requires a stale publication past DB-clock grace';
        END IF;
        NEW.stale_since := OLD.stale_since;
    ELSIF NOT lease_is_current THEN
        RAISE EXCEPTION 'publication requires current unexpired uncancelled lease';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER publication_current_lease_guard
    BEFORE INSERT OR UPDATE ON artifact_publications
    FOR EACH ROW EXECUTE FUNCTION ensure_publication_current_lease();

CREATE FUNCTION ensure_publication_validated_blob() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IN ('validated', 'finalized') AND NOT EXISTS (
        SELECT 1 FROM blobs b
        WHERE b.id = NEW.canonical_blob_id AND b.state = 'verified'
          AND b.sha256 = NEW.observed_sha256
          AND b.size_bytes = NEW.observed_size_bytes
          AND b.content_type = NEW.observed_content_type
          AND b.storage_key = 'blobs/sha256/' || substring(b.sha256 from 1 for 2) || '/'
              || substring(b.sha256 from 3 for 2) || '/' || b.sha256
    ) THEN
        RAISE EXCEPTION 'VALIDATED publication requires matching canonical VERIFIED blob';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER publication_validated_blob_guard
    BEFORE INSERT OR UPDATE ON artifact_publications
    FOR EACH ROW EXECUTE FUNCTION ensure_publication_validated_blob();

CREATE FUNCTION prevent_publication_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'publication ledger is append-preserving and cannot be deleted';
END;
$$;
CREATE TRIGGER publication_delete_guard
    BEFORE DELETE ON artifact_publications
    FOR EACH ROW EXECUTE FUNCTION prevent_publication_delete();

CREATE FUNCTION ensure_publication_artifact_proof() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    publication_id_value UUID;
    artifact_job_id UUID;
    artifact_attempt_id UUID;
    artifact_kind TEXT;
    artifact_blob_id UUID;
BEGIN
    publication_id_value := CASE WHEN TG_TABLE_NAME = 'artifact_publications'
        THEN (to_jsonb(NEW)->>'id')::UUID
        ELSE (to_jsonb(NEW)->>'publication_id')::UUID
    END;
    IF EXISTS (
        SELECT 1 FROM artifact_publications p
        WHERE p.id = publication_id_value AND p.state = 'finalized'
    ) AND NOT EXISTS (
        SELECT 1
        FROM artifact_publications p
        JOIN artifacts a ON a.publication_id = p.id
        JOIN blobs b ON b.id = a.blob_id
        WHERE p.id = publication_id_value AND a.job_id = p.job_id
          AND a.attempt_id = p.attempt_id AND a.kind = p.kind
          AND a.blob_id = p.canonical_blob_id AND a.state = 'ready' AND b.state = 'verified'
    ) THEN
        RAISE EXCEPTION 'FINALIZED publication requires matching READY artifact';
    END IF;
    IF TG_TABLE_NAME = 'artifacts' THEN
        artifact_job_id := (to_jsonb(NEW)->>'job_id')::UUID;
        artifact_attempt_id := (to_jsonb(NEW)->>'attempt_id')::UUID;
        artifact_kind := to_jsonb(NEW)->>'kind';
        artifact_blob_id := (to_jsonb(NEW)->>'blob_id')::UUID;
        IF NOT EXISTS (
            SELECT 1 FROM artifact_publications p
            WHERE p.id = publication_id_value AND p.state = 'finalized'
              AND p.job_id = artifact_job_id AND p.attempt_id = artifact_attempt_id
              AND p.kind = artifact_kind AND p.canonical_blob_id = artifact_blob_id
        ) THEN
            RAISE EXCEPTION 'artifact requires matching FINALIZED publication';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER publication_artifact_proof_from_publication
    AFTER INSERT OR UPDATE ON artifact_publications
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ensure_publication_artifact_proof();
CREATE CONSTRAINT TRIGGER publication_artifact_proof_from_artifact
    AFTER INSERT OR UPDATE ON artifacts
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ensure_publication_artifact_proof();

CREATE FUNCTION ensure_succeeded_publication_proof() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'succeeded' AND (
        EXISTS (
            SELECT kind FROM artifact_publications
            WHERE job_id = NEW.id AND attempt_id = NEW.winning_attempt_id AND state = 'finalized'
            EXCEPT SELECT unnest(NEW.required_artifact_kinds)
        )
        OR EXISTS (
            SELECT unnest(NEW.required_artifact_kinds)
            EXCEPT SELECT kind FROM artifact_publications
            WHERE job_id = NEW.id AND attempt_id = NEW.winning_attempt_id AND state = 'finalized'
        )
        OR (SELECT count(*) FROM job_events
            WHERE job_id = NEW.id AND attempt_id = NEW.winning_attempt_id
              AND event_type = 'job_succeeded') <> 1
    ) THEN
        RAISE EXCEPTION 'SUCCEEDED requires exact FINALIZED publications and one success event';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER succeeded_publication_proof_guard
    AFTER INSERT OR UPDATE ON jobs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ensure_succeeded_publication_proof();
