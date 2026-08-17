-- Phase 3A PostgreSQL production migration. This byte-identical file is both
-- the packaged durable/sql resource and the repository-root development mirror.
-- PostgreSQL, not the in-memory test repository, is authoritative in production.

CREATE TYPE deployment_state AS ENUM ('registered', 'ready', 'unhealthy', 'retired');
CREATE TYPE blob_state AS ENUM ('verified', 'quarantined', 'deleted');
CREATE TYPE asset_state AS ENUM ('verified', 'rejected', 'deleted');
CREATE TYPE job_state AS ENUM ('queued', 'running', 'succeeded', 'failed', 'cancelled');
CREATE TYPE attempt_state AS ENUM ('active', 'succeeded', 'failed_retryable', 'failed_terminal', 'cancelled');
CREATE TYPE artifact_state AS ENUM ('ready', 'rejected');
CREATE TYPE worker_state AS ENUM ('ready', 'busy', 'unhealthy');

CREATE TABLE model_deployments (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    adapter_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    state deployment_state NOT NULL DEFAULT 'registered',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (length(trim(name)) > 0 AND length(trim(adapter_id)) > 0)
);

CREATE TABLE blobs (
    id UUID PRIMARY KEY,
    sha256 CHAR(64) NOT NULL UNIQUE,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    storage_key TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    state blob_state NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE assets (
    id UUID PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    blob_id UUID NOT NULL REFERENCES blobs(id),
    kind TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    state asset_state NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION valid_required_artifact_kinds(kinds TEXT[]) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE AS $$
    SELECT cardinality(kinds) > 0
       AND kinds <@ ARRAY['output', 'manifest']::TEXT[]
       AND cardinality(kinds) = cardinality(ARRAY(SELECT DISTINCT unnest(kinds)))
$$;

CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 CHAR(64) NOT NULL,
    request_json JSONB NOT NULL,
    model_deployment_id UUID NOT NULL REFERENCES model_deployments(id),
    required_artifact_kinds TEXT[] NOT NULL DEFAULT ARRAY['output'],
    state job_state NOT NULL DEFAULT 'queued',
    lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    current_attempt_id UUID NULL,
    winning_attempt_id UUID NULL,
    cancel_requested_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace_id, idempotency_key),
    CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (length(trim(namespace_id)) > 0 AND length(trim(idempotency_key)) > 0),
    CHECK (valid_required_artifact_kinds(required_artifact_kinds))
);

CREATE TABLE job_inputs (
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    asset_id UUID NOT NULL REFERENCES assets(id),
    PRIMARY KEY (job_id, role)
);

CREATE TABLE workers (
    id UUID PRIMARY KEY,
    model_deployment_id UUID NOT NULL REFERENCES model_deployments(id),
    state worker_state NOT NULL DEFAULT 'ready',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE job_attempts (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    worker_id UUID NOT NULL REFERENCES workers(id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    lease_epoch BIGINT NOT NULL CHECK (lease_epoch > 0),
    state attempt_state NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ NULL,
    UNIQUE (job_id, attempt_no),
    UNIQUE (job_id, id)
);

ALTER TABLE jobs
    ADD CONSTRAINT jobs_current_attempt_fk
    FOREIGN KEY (id, current_attempt_id) REFERENCES job_attempts(job_id, id)
    DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT jobs_winning_attempt_fk
    FOREIGN KEY (id, winning_attempt_id) REFERENCES job_attempts(job_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE artifacts (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_id UUID NOT NULL,
    blob_id UUID NOT NULL REFERENCES blobs(id),
    kind TEXT NOT NULL,
    state artifact_state NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, kind),
    CHECK (kind IN ('output', 'manifest')),
    FOREIGN KEY (job_id, attempt_id) REFERENCES job_attempts(job_id, id)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE job_events (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_id UUID NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, attempt_id) REFERENCES job_attempts(job_id, id)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE UNIQUE INDEX one_active_attempt_per_job
    ON job_attempts (job_id) WHERE state = 'active';
CREATE INDEX jobs_queued_idx ON jobs (created_at, id) WHERE state = 'queued';
CREATE INDEX artifacts_job_idx ON artifacts (job_id, state);

-- Cross-table invariants need a trigger rather than CHECK constraints.
CREATE FUNCTION ensure_verified_job_input() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM assets a
        JOIN blobs b ON b.id = a.blob_id
        JOIN jobs j ON j.id = NEW.job_id
        WHERE a.id = NEW.asset_id AND a.namespace_id = j.namespace_id
          AND a.state = 'verified' AND b.state = 'verified'
    ) THEN
        RAISE EXCEPTION 'only VERIFIED assets with VERIFIED blobs may enter jobs';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_inputs_must_be_verified
    BEFORE INSERT OR UPDATE ON job_inputs
    FOR EACH ROW EXECUTE FUNCTION ensure_verified_job_input();

CREATE FUNCTION ensure_ready_artifact_blob() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'ready' AND NOT EXISTS (
        SELECT 1 FROM blobs b WHERE b.id = NEW.blob_id AND b.state = 'verified'
    ) THEN
        RAISE EXCEPTION 'READY artifact requires a VERIFIED blob';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ready_artifact_requires_verified_blob
    BEFORE INSERT OR UPDATE OF state, blob_id ON artifacts
    FOR EACH ROW EXECUTE FUNCTION ensure_ready_artifact_blob();

CREATE FUNCTION prevent_job_input_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'job inputs are immutable once inserted';
END;
$$;
CREATE TRIGGER job_inputs_immutable
    BEFORE UPDATE OR DELETE ON job_inputs
    FOR EACH ROW EXECUTE FUNCTION prevent_job_input_mutation();

CREATE FUNCTION ensure_verified_asset_blob() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'verified' AND NOT EXISTS (
        SELECT 1 FROM blobs b WHERE b.id = NEW.blob_id AND b.state = 'verified'
    ) THEN
        RAISE EXCEPTION 'a VERIFIED asset requires a VERIFIED blob';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER verified_asset_requires_verified_blob
    BEFORE INSERT OR UPDATE OF state, blob_id ON assets
    FOR EACH ROW EXECUTE FUNCTION ensure_verified_asset_blob();

CREATE FUNCTION prevent_deployment_fingerprint_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fingerprint IS DISTINCT FROM OLD.fingerprint THEN
        RAISE EXCEPTION 'model deployment fingerprint is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER deployment_fingerprint_immutable
    BEFORE UPDATE OF fingerprint ON model_deployments
    FOR EACH ROW EXECUTE FUNCTION prevent_deployment_fingerprint_change();

CREATE FUNCTION prevent_deployment_identity_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.name IS DISTINCT FROM OLD.name OR NEW.adapter_id IS DISTINCT FROM OLD.adapter_id
       OR NEW.manifest IS DISTINCT FROM OLD.manifest THEN
        RAISE EXCEPTION 'model deployment identity and manifest are immutable';
    END IF;
    IF OLD.state = 'retired' AND NEW.state <> 'retired' THEN
        RAISE EXCEPTION 'RETIRED deployment cannot be reactivated';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER deployment_identity_immutable
    BEFORE UPDATE ON model_deployments
    FOR EACH ROW EXECUTE FUNCTION prevent_deployment_identity_mutation();

CREATE FUNCTION prevent_blob_identity_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.sha256 IS DISTINCT FROM OLD.sha256 OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
       OR NEW.storage_key IS DISTINCT FROM OLD.storage_key OR NEW.content_type IS DISTINCT FROM OLD.content_type THEN
        RAISE EXCEPTION 'content-addressed blob identity is immutable';
    END IF;
    IF OLD.state = 'deleted' AND NEW.state <> 'deleted' THEN
        RAISE EXCEPTION 'DELETED blob cannot be restored';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER blob_identity_immutable
    BEFORE UPDATE ON blobs
    FOR EACH ROW EXECUTE FUNCTION prevent_blob_identity_mutation();

CREATE FUNCTION prevent_asset_identity_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.namespace_id IS DISTINCT FROM OLD.namespace_id OR NEW.blob_id IS DISTINCT FROM OLD.blob_id
       OR NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'asset namespace, blob, and kind are immutable';
    END IF;
    IF OLD.state = 'deleted' AND NEW.state <> 'deleted' THEN
        RAISE EXCEPTION 'DELETED asset cannot be restored';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER asset_identity_immutable
    BEFORE UPDATE ON assets
    FOR EACH ROW EXECUTE FUNCTION prevent_asset_identity_mutation();

CREATE FUNCTION prevent_attempt_identity_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.job_id IS DISTINCT FROM OLD.job_id OR NEW.worker_id IS DISTINCT FROM OLD.worker_id
       OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no OR NEW.lease_epoch IS DISTINCT FROM OLD.lease_epoch THEN
        RAISE EXCEPTION 'attempt identity and epoch are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER attempt_identity_immutable
    BEFORE UPDATE ON job_attempts
    FOR EACH ROW EXECUTE FUNCTION prevent_attempt_identity_mutation();

CREATE FUNCTION ensure_attempt_terminal_immutability() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'active' AND NEW.finished_at IS NOT NULL THEN
        RAISE EXCEPTION 'ACTIVE attempt cannot have finished_at';
    END IF;
    IF NEW.state <> 'active' AND NEW.finished_at IS NULL THEN
        RAISE EXCEPTION 'terminal attempt requires finished_at';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.state <> 'active' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal attempt is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER attempts_terminal_immutable
    BEFORE INSERT OR UPDATE ON job_attempts
    FOR EACH ROW EXECUTE FUNCTION ensure_attempt_terminal_immutability();

CREATE FUNCTION prevent_worker_deployment_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.model_deployment_id IS DISTINCT FROM OLD.model_deployment_id THEN
        RAISE EXCEPTION 'worker deployment identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER worker_deployment_immutable
    BEFORE UPDATE ON workers
    FOR EACH ROW EXECUTE FUNCTION prevent_worker_deployment_mutation();

CREATE FUNCTION prevent_job_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'job events are append-only';
END;
$$;
CREATE TRIGGER job_events_append_only
    BEFORE UPDATE OR DELETE ON job_events
    FOR EACH ROW EXECUTE FUNCTION prevent_job_event_mutation();

CREATE FUNCTION prevent_job_semantic_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.namespace_id IS DISTINCT FROM OLD.namespace_id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.request_sha256 IS DISTINCT FROM OLD.request_sha256
       OR NEW.request_json IS DISTINCT FROM OLD.request_json
       OR NEW.model_deployment_id IS DISTINCT FROM OLD.model_deployment_id
       OR NEW.required_artifact_kinds IS DISTINCT FROM OLD.required_artifact_kinds THEN
        RAISE EXCEPTION 'job request semantics are immutable';
    END IF;
    IF OLD.state IN ('succeeded', 'failed', 'cancelled') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal jobs are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_semantics_and_terminal_state_immutable
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION prevent_job_semantic_mutation();

CREATE FUNCTION ensure_job_success_artifacts() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.state = 'succeeded'
       AND NEW.required_artifact_kinds IS DISTINCT FROM OLD.required_artifact_kinds THEN
        RAISE EXCEPTION 'required artifact kinds of SUCCEEDED job are immutable';
    END IF;
    IF NEW.state = 'succeeded' THEN
        IF NEW.current_attempt_id IS NOT NULL OR NEW.winning_attempt_id IS NULL
           OR NEW.cancel_requested_at IS NOT NULL
           OR NOT EXISTS (
                SELECT 1 FROM job_attempts a
                WHERE a.job_id = NEW.id AND a.id = NEW.winning_attempt_id
                  AND a.state = 'succeeded' AND a.lease_epoch = NEW.lease_epoch
           )
           OR EXISTS (
                SELECT required_kind
                FROM unnest(NEW.required_artifact_kinds) AS required_kind
                EXCEPT
                SELECT a.kind
                FROM artifacts a JOIN blobs b ON b.id = a.blob_id
                WHERE a.job_id = NEW.id AND a.attempt_id = NEW.winning_attempt_id
                  AND a.state = 'ready' AND b.state = 'verified'
           ) THEN
            RAISE EXCEPTION 'SUCCEEDED requires all READY verified artifacts from winning attempt';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER jobs_succeeded_requires_ready_artifacts
    BEFORE INSERT OR UPDATE OF state, current_attempt_id, winning_attempt_id,
        cancel_requested_at, required_artifact_kinds, lease_epoch ON jobs
    FOR EACH ROW EXECUTE FUNCTION ensure_job_success_artifacts();

CREATE FUNCTION ensure_job_lifecycle_shape() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'queued' AND (NEW.current_attempt_id IS NOT NULL OR NEW.winning_attempt_id IS NOT NULL) THEN
        RAISE EXCEPTION 'QUEUED job cannot have current or winning attempt';
    ELSIF NEW.state = 'running' AND (NEW.current_attempt_id IS NULL OR NEW.winning_attempt_id IS NOT NULL) THEN
        RAISE EXCEPTION 'RUNNING job requires current attempt and no winner';
    ELSIF NEW.state IN ('failed', 'cancelled')
          AND (NEW.current_attempt_id IS NOT NULL OR NEW.winning_attempt_id IS NOT NULL) THEN
        RAISE EXCEPTION 'terminal non-success job cannot have current or winning attempt';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER jobs_lifecycle_shape_guard
    BEFORE INSERT OR UPDATE OF state, current_attempt_id, winning_attempt_id ON jobs
    FOR EACH ROW EXECUTE FUNCTION ensure_job_lifecycle_shape();

-- Once a job is SUCCEEDED, the proof above must remain true.  The successful
-- attempt and its required artifact rows are therefore immutable, and a blob
-- that supplies such an artifact cannot be downgraded from VERIFIED.
CREATE FUNCTION prevent_winning_attempt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM jobs j
        WHERE j.winning_attempt_id = OLD.id AND j.state = 'succeeded'
    ) THEN
        RAISE EXCEPTION 'winning attempt of SUCCEEDED job is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER winning_attempt_of_succeeded_job_immutable
    BEFORE UPDATE ON job_attempts
    FOR EACH ROW EXECUTE FUNCTION prevent_winning_attempt_mutation();

CREATE FUNCTION prevent_succeeded_artifact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected_job_id UUID;
BEGIN
    affected_job_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END;
    IF EXISTS (SELECT 1 FROM jobs j WHERE j.id = affected_job_id AND j.state = 'succeeded') THEN
        RAISE EXCEPTION 'artifacts of SUCCEEDED job are immutable';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
CREATE TRIGGER succeeded_artifact_immutable_on_insert
    BEFORE INSERT ON artifacts
    FOR EACH ROW EXECUTE FUNCTION prevent_succeeded_artifact_mutation();
CREATE TRIGGER succeeded_artifact_immutable_on_update
    BEFORE UPDATE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION prevent_succeeded_artifact_mutation();
CREATE TRIGGER succeeded_artifact_immutable_on_delete
    BEFORE DELETE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION prevent_succeeded_artifact_mutation();

CREATE FUNCTION prevent_artifact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'artifacts are immutable once written';
END;
$$;
CREATE TRIGGER artifacts_append_only
    BEFORE UPDATE OR DELETE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION prevent_artifact_mutation();

CREATE FUNCTION prevent_verified_blob_downgrade() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM artifacts a JOIN jobs j ON j.id = a.job_id
        WHERE a.blob_id = OLD.id AND a.state = 'ready' AND j.state = 'succeeded'
    ) THEN
        RAISE EXCEPTION 'blob backs a READY artifact of a SUCCEEDED job and is immutable';
    END IF;
    IF OLD.state = 'verified' AND NEW.state <> 'verified' THEN
        IF EXISTS (
            SELECT 1
            FROM assets a
            JOIN job_inputs i ON i.asset_id = a.id
            JOIN jobs j ON j.id = i.job_id
            WHERE a.blob_id = OLD.id AND j.state IN ('queued', 'running')
        ) THEN
            RAISE EXCEPTION 'verified blob backs an active job input';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER verified_blob_downgrade_guard
    BEFORE UPDATE ON blobs
    FOR EACH ROW EXECUTE FUNCTION prevent_verified_blob_downgrade();

CREATE FUNCTION prevent_verified_asset_downgrade() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'verified' AND NEW.state <> 'verified' AND EXISTS (
        SELECT 1
        FROM job_inputs i JOIN jobs j ON j.id = i.job_id
        WHERE i.asset_id = OLD.id AND j.state IN ('queued', 'running')
    ) THEN
        RAISE EXCEPTION 'verified asset backs an active job input';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER verified_asset_downgrade_guard
    BEFORE UPDATE OF state ON assets
    FOR EACH ROW EXECUTE FUNCTION prevent_verified_asset_downgrade();

-- A transaction may insert an ACTIVE attempt then make its job RUNNING (or
-- finish an attempt then clear current_attempt_id).  Defer this bidirectional
-- proof to commit so that construction remains atomic but no broken graph can
-- persist.  This deliberately has no lease claiming or reaper behaviour.
CREATE FUNCTION ensure_job_attempt_linkage() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected_job_id UUID;
    job_row jobs%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'jobs' THEN
        IF TG_OP = 'DELETE' THEN
            affected_job_id := (to_jsonb(OLD)->>'id')::UUID;
        ELSE
            affected_job_id := (to_jsonb(NEW)->>'id')::UUID;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        affected_job_id := (to_jsonb(OLD)->>'job_id')::UUID;
    ELSE
        affected_job_id := (to_jsonb(NEW)->>'job_id')::UUID;
    END IF;
    SELECT * INTO job_row FROM jobs WHERE id = affected_job_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF job_row.state = 'running' AND NOT EXISTS (
        SELECT 1 FROM job_attempts a
        WHERE a.id = job_row.current_attempt_id AND a.job_id = job_row.id AND a.state = 'active'
    ) THEN
        RAISE EXCEPTION 'RUNNING job requires its current ACTIVE attempt';
    END IF;
    IF EXISTS (
        SELECT 1 FROM job_attempts a
        WHERE a.job_id = job_row.id AND a.state = 'active'
          AND (job_row.state <> 'running' OR job_row.current_attempt_id IS DISTINCT FROM a.id)
    ) THEN
        RAISE EXCEPTION 'ACTIVE attempt must be the current attempt of its RUNNING job';
    END IF;
    IF job_row.current_attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM job_attempts a
        WHERE a.id = job_row.current_attempt_id AND a.job_id = job_row.id AND a.state = 'active'
    ) THEN
        RAISE EXCEPTION 'current attempt must be ACTIVE';
    END IF;
    IF job_row.winning_attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM job_attempts a
        WHERE a.id = job_row.winning_attempt_id AND a.job_id = job_row.id AND a.state = 'succeeded'
    ) THEN
        RAISE EXCEPTION 'winning attempt must be SUCCEEDED';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER jobs_attempt_linkage_guard
    AFTER INSERT OR UPDATE OR DELETE ON jobs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ensure_job_attempt_linkage();
CREATE CONSTRAINT TRIGGER attempts_job_linkage_guard
    AFTER INSERT OR UPDATE OR DELETE ON job_attempts
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ensure_job_attempt_linkage();
