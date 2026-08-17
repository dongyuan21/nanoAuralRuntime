-- Phase 3B verified upload authority. This byte-identical file is both the
-- packaged durable/sql resource and repository-root development mirror.

CREATE TYPE upload_mode AS ENUM ('single', 'multipart');
CREATE TYPE upload_state AS ENUM ('initiated', 'uploaded', 'verifying', 'verified', 'rejected', 'expired');

CREATE TABLE upload_sessions (
    id UUID PRIMARY KEY,
    namespace_id TEXT NOT NULL CHECK (length(trim(namespace_id)) > 0),
    mode upload_mode NOT NULL,
    expected_size_bytes BIGINT NOT NULL CHECK (expected_size_bytes >= 0),
    expected_sha256 CHAR(64) NULL CHECK (expected_sha256 IS NULL OR expected_sha256 ~ '^[0-9a-f]{64}$'),
    staging_key TEXT NOT NULL UNIQUE CHECK (staging_key ~ '^staging/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    state upload_state NOT NULL DEFAULT 'initiated',
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    verification_started_at TIMESTAMPTZ NULL,
    verified_blob_id UUID NULL REFERENCES blobs(id),
    verified_asset_id UUID NULL REFERENCES assets(id),
    rejection_reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at TIMESTAMPTZ NULL,
    CHECK ((state = 'verified') = (verified_blob_id IS NOT NULL AND verified_asset_id IS NOT NULL)),
    CHECK (state = 'verified' OR (verified_blob_id IS NULL AND verified_asset_id IS NULL)),
    CHECK (state <> 'rejected' OR rejection_reason IS NOT NULL),
    CHECK (state = 'rejected' OR rejection_reason IS NULL),
    CHECK (state IN ('verified','rejected','expired') OR finalized_at IS NULL),
    CHECK (state NOT IN ('verified','rejected','expired') OR finalized_at IS NOT NULL),
    CHECK (state NOT IN ('initiated','uploaded') OR verification_started_at IS NULL),
    CHECK (state <> 'verifying' OR verification_started_at IS NOT NULL)
    ,CHECK (expires_at > created_at)
);

CREATE INDEX upload_sessions_expiry_idx ON upload_sessions (expires_at) WHERE state IN ('initiated', 'uploaded', 'verifying');

CREATE FUNCTION ensure_upload_verified_asset() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.state <> 'expired' AND clock_timestamp() >= OLD.expires_at THEN
        RAISE EXCEPTION 'expired upload session cannot advance';
    END IF;
    IF NEW.state = 'verified' AND NOT EXISTS (
        SELECT 1 FROM assets a JOIN blobs b ON b.id = a.blob_id
        WHERE a.id = NEW.verified_asset_id AND a.blob_id = NEW.verified_blob_id
          AND a.namespace_id = NEW.namespace_id AND a.state = 'verified' AND b.state = 'verified'
    ) THEN
        RAISE EXCEPTION 'VERIFIED upload requires namespace VERIFIED asset and blob';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.state IN ('verified', 'rejected', 'expired') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal upload session is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER upload_verified_asset_guard
    BEFORE INSERT OR UPDATE ON upload_sessions
    FOR EACH ROW EXECUTE FUNCTION ensure_upload_verified_asset();

CREATE FUNCTION ensure_upload_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' AND (NEW.state <> 'initiated' OR NEW.version <> 0) THEN
        RAISE EXCEPTION 'upload session must begin INITIATED at version zero';
    ELSIF TG_OP = 'UPDATE' AND (
        NEW.version <> OLD.version + 1 OR NOT (
          (OLD.state = 'initiated' AND NEW.state IN ('uploaded','expired')) OR
          (OLD.state = 'uploaded' AND NEW.state IN ('verifying','expired')) OR
          (OLD.state = 'verifying' AND NEW.state = 'verifying' AND NEW.verification_started_at > OLD.verification_started_at) OR
          (OLD.state = 'verifying' AND NEW.state IN ('verified','rejected','expired'))
        )
    ) THEN
        RAISE EXCEPTION 'invalid upload state/version transition';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER upload_transition_guard
    BEFORE INSERT OR UPDATE ON upload_sessions
    FOR EACH ROW EXECUTE FUNCTION ensure_upload_transition();

CREATE FUNCTION prevent_upload_identity_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.namespace_id IS DISTINCT FROM OLD.namespace_id OR NEW.mode IS DISTINCT FROM OLD.mode OR NEW.expected_size_bytes IS DISTINCT FROM OLD.expected_size_bytes OR NEW.expected_sha256 IS DISTINCT FROM OLD.expected_sha256 OR NEW.staging_key IS DISTINCT FROM OLD.staging_key OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN RAISE EXCEPTION 'upload identity is immutable'; END IF;
 IF NEW.state IN ('verified','rejected','expired') AND NEW.finalized_at IS NULL THEN RAISE EXCEPTION 'terminal upload requires finalized_at'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER upload_identity_immutable BEFORE UPDATE ON upload_sessions FOR EACH ROW EXECUTE FUNCTION prevent_upload_identity_mutation();
