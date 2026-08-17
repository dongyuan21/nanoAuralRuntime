-- Phase 3C queue/lease authority.  PostgreSQL's clock is the sole lease clock.
-- This file is byte-identical to the packaged migration mirror.
ALTER TABLE workers ADD COLUMN last_heartbeat_at TIMESTAMPTZ NULL;
ALTER TABLE jobs ADD COLUMN retry_not_before TIMESTAMPTZ NULL;
ALTER TABLE job_attempts
    ADD COLUMN heartbeat_at TIMESTAMPTZ NULL,
    ADD COLUMN lease_expires_at TIMESTAMPTZ NULL,
    ADD COLUMN failure_reason TEXT NULL,
    ADD CONSTRAINT attempts_lease_shape_check CHECK (
        (state = 'active' AND heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL
         AND lease_expires_at > heartbeat_at
         AND finished_at IS NULL)
        OR
        (state <> 'active' AND heartbeat_at IS NULL AND lease_expires_at IS NULL
         AND finished_at IS NOT NULL)
    );

-- A worker is a single-lease resource, as well as a job having one active attempt.
CREATE UNIQUE INDEX one_active_attempt_per_worker
    ON job_attempts (worker_id) WHERE state = 'active';
CREATE INDEX active_attempt_lease_expiry_idx
    ON job_attempts (lease_expires_at) WHERE state = 'active';
CREATE INDEX jobs_claimable_idx
    ON jobs (model_deployment_id, retry_not_before, created_at, id) WHERE state = 'queued';
CREATE INDEX ready_workers_deployment_idx
    ON workers (model_deployment_id, id) WHERE state = 'ready';

CREATE FUNCTION ensure_attempt_lease_shape() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'active' THEN
        IF NEW.heartbeat_at IS NULL OR NEW.lease_expires_at IS NULL
           OR NEW.finished_at IS NOT NULL THEN
            RAISE EXCEPTION 'ACTIVE attempt requires heartbeat and expiry but no finished_at';
        END IF;
    ELSIF NEW.lease_expires_at IS NOT NULL OR NEW.heartbeat_at IS NOT NULL THEN
        RAISE EXCEPTION 'terminal attempt must clear lease fields';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER attempts_lease_shape_guard
    BEFORE INSERT OR UPDATE OF state, heartbeat_at, lease_expires_at, finished_at ON job_attempts
    FOR EACH ROW EXECUTE FUNCTION ensure_attempt_lease_shape();

CREATE FUNCTION prevent_job_lease_epoch_decrease() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lease_epoch < OLD.lease_epoch THEN
        RAISE EXCEPTION 'job lease epoch cannot decrease';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_lease_epoch_monotonic
    BEFORE UPDATE OF lease_epoch ON jobs
    FOR EACH ROW EXECUTE FUNCTION prevent_job_lease_epoch_decrease();

CREATE FUNCTION ensure_current_attempt_lease_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    job_id_value UUID;
BEGIN
    job_id_value := CASE WHEN TG_TABLE_NAME = 'jobs'
        THEN (to_jsonb(NEW)->>'id')::UUID
        ELSE (to_jsonb(NEW)->>'job_id')::UUID
    END;
    IF EXISTS (
        SELECT 1 FROM jobs j WHERE j.id = job_id_value AND j.state = 'running'
    ) AND NOT EXISTS (
        SELECT 1 FROM jobs j JOIN job_attempts a ON a.id=j.current_attempt_id
        WHERE j.id=job_id_value AND a.state='active' AND a.lease_epoch=j.lease_epoch
    ) THEN
        RAISE EXCEPTION 'RUNNING job current attempt must have matching lease epoch';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER jobs_current_attempt_epoch_guard
    AFTER INSERT OR UPDATE OF state,current_attempt_id,lease_epoch ON jobs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ensure_current_attempt_lease_epoch();
CREATE CONSTRAINT TRIGGER attempts_current_job_epoch_guard
    AFTER INSERT OR UPDATE OF state,lease_epoch ON job_attempts
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ensure_current_attempt_lease_epoch();
