"""PostgreSQL authority for Phase 3B upload sessions."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional, Tuple

from .domain import AssetRecord, BlobRecord
from .errors import IdempotencyConflictError, NotFoundError, StateTransitionError
from .uploads import UploadMode, UploadSession, UploadState


class PostgresUploadRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def create_session(self, session: UploadSession) -> UploadSession:
        with self._connection.transaction():
            self._connection.execute(
                """INSERT INTO upload_sessions
                   (id,namespace_id,mode,expected_size_bytes,expected_sha256,staging_key,state,version,expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    session.session_id,
                    session.namespace_id,
                    session.mode.value,
                    session.expected_size_bytes,
                    session.expected_sha256,
                    session.staging_key,
                    session.state.value,
                    session.version,
                    session.expires_at,
                ),
            )
        return session

    def get_session(self, session_id: str) -> UploadSession:
        row = self._connection.execute(
            """SELECT id,namespace_id,mode,expected_size_bytes,staging_key,expires_at,expected_sha256,state,
                      version,verified_blob_id,verified_asset_id,rejection_reason
               FROM upload_sessions WHERE id=%s""",
            (session_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("upload session not found: {0}".format(session_id))
        return UploadSession(
            str(row[0]),
            row[1],
            UploadMode(row[2]),
            row[3],
            row[4],
            row[5],
            row[6],
            UploadState(row[7]),
            row[8],
            str(row[9]) if row[9] else None,
            str(row[10]) if row[10] else None,
            row[11],
        )

    def mark_uploaded(self, session_id: str, expected_version: int) -> UploadSession:
        return self._cas_state(
            session_id, expected_version, UploadState.INITIATED, UploadState.UPLOADED
        )

    def claim_verification(self, session_id: str, expected_version: int) -> UploadSession:
        with self._connection.transaction():
            row = self._connection.execute(
                """UPDATE upload_sessions SET state='verifying',verification_started_at=clock_timestamp(),version=version+1
                   WHERE id=%s AND version=%s AND state='uploaded' AND clock_timestamp() < expires_at RETURNING id""",
                (session_id, expected_version),
            ).fetchone()
            if row is None:
                raise StateTransitionError("upload session CAS rejected")
        return self.get_session(session_id)

    def reclaim_verification(
        self, session_id: str, expected_version: int, stale_before: datetime
    ) -> UploadSession:
        with self._connection.transaction():
            row = self._connection.execute(
                """UPDATE upload_sessions SET verification_started_at=clock_timestamp(),version=version+1
                   WHERE id=%s AND version=%s AND state='verifying' AND clock_timestamp() < expires_at AND verification_started_at < %s RETURNING id""",
                (session_id, expected_version, stale_before),
            ).fetchone()
            if row is None:
                raise StateTransitionError("verification reclaim CAS rejected")
        return self.get_session(session_id)

    def reject(self, session_id: str, expected_version: int, reason: str) -> UploadSession:
        if not isinstance(reason, str) or not reason:
            raise ValueError("rejection reason must be non-empty")
        with self._connection.transaction():
            row = self._connection.execute(
                """UPDATE upload_sessions SET state='rejected',rejection_reason=%s,finalized_at=clock_timestamp(),version=version+1
                   WHERE id=%s AND version=%s AND state='verifying' AND clock_timestamp() < expires_at RETURNING id""",
                (reason, session_id, expected_version),
            ).fetchone()
            if row is None:
                raise StateTransitionError("upload session CAS rejected")
        return self.get_session(session_id)

    def finalize_verified(
        self, session_id: str, expected_version: int, blob: BlobRecord, asset: AssetRecord
    ) -> UploadSession:
        with self._connection.transaction():
            session = self._locked(session_id)
            if session.version != expected_version or session.state != UploadState.VERIFYING:
                raise StateTransitionError("upload session CAS rejected")
            if asset.blob_id != blob.blob_id or asset.namespace_id != session.namespace_id:
                raise StateTransitionError("verified asset does not match upload session")
            self._connection.execute(
                """INSERT INTO blobs (id,sha256,size_bytes,storage_key,content_type,state)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (sha256) DO NOTHING""",
                (
                    blob.blob_id,
                    blob.sha256,
                    blob.size_bytes,
                    blob.storage_key,
                    blob.content_type,
                    blob.state.value,
                ),
            )
            blob_row = self._connection.execute(
                "SELECT id,size_bytes,storage_key,content_type,state FROM blobs WHERE sha256=%s",
                (blob.sha256,),
            ).fetchone()
            if blob_row is None or tuple(blob_row[1:]) != (
                blob.size_bytes,
                blob.storage_key,
                blob.content_type,
                blob.state.value,
            ):
                raise IdempotencyConflictError("canonical blob digest has different metadata")
            actual_blob_id = str(blob_row[0])
            self._connection.execute(
                """INSERT INTO assets (id,namespace_id,blob_id,kind,metadata,state) VALUES (%s,%s,%s,%s,%s::jsonb,%s)""",
                (
                    asset.asset_id,
                    asset.namespace_id,
                    actual_blob_id,
                    asset.kind.value,
                    json.dumps(dict(asset.metadata), sort_keys=True),
                    asset.state.value,
                ),
            )
            row = self._connection.execute(
                """UPDATE upload_sessions SET state='verified',verified_blob_id=%s,verified_asset_id=%s,
                   finalized_at=clock_timestamp(),version=version+1 WHERE id=%s AND version=%s AND state='verifying' AND clock_timestamp() < expires_at RETURNING id""",
                (actual_blob_id, asset.asset_id, session_id, expected_version),
            ).fetchone()
            if row is None:
                raise StateTransitionError("upload session CAS rejected")
        return self.get_session(session_id)

    def expire_before(self, now: datetime) -> Tuple[UploadSession, ...]:
        # PostgreSQL is the production clock authority.  Keep ``now`` in the
        # protocol for the deterministic in-memory implementation, but never
        # let an application-supplied timestamp expire or retain a DB session.
        del now
        with self._connection.transaction():
            rows = self._connection.execute(
                """UPDATE upload_sessions SET state='expired',finalized_at=clock_timestamp(),version=version+1
                   WHERE expires_at < clock_timestamp() AND state IN ('initiated','uploaded','verifying')
                   RETURNING id"""
            ).fetchall()
        return tuple(self.get_session(str(row[0])) for row in rows)

    def expire_batch(self, limit: int) -> Tuple[UploadSession, ...]:
        """Expire at most ``limit`` overdue sessions using PostgreSQL's clock.

        Candidate locks and the deterministic expiry/id order let independent
        recovery processes divide work without application-clock authority or
        an unbounded table update.
        """

        self._positive_limit(limit)
        with self._connection.transaction():
            rows = self._connection.execute(
                """WITH candidates AS (
                       SELECT id FROM upload_sessions
                       WHERE expires_at < clock_timestamp()
                         AND state IN ('initiated','uploaded','verifying')
                       ORDER BY expires_at,id
                       FOR UPDATE SKIP LOCKED LIMIT %s
                   ), expired AS (
                       UPDATE upload_sessions AS session
                       SET state='expired',finalized_at=clock_timestamp(),version=version+1
                       FROM candidates WHERE session.id=candidates.id
                       RETURNING session.id,session.expires_at
                   )
                   SELECT id FROM expired ORDER BY expires_at,id""",
                (limit,),
            ).fetchall()
        return tuple(self.get_session(str(row[0])) for row in rows)

    def terminal_staging_candidates(self, limit: Optional[int] = None) -> Tuple[UploadSession, ...]:
        """Return stable terminal sessions lacking staging-deletion evidence.

        ``None`` retains the Phase 3B compatibility surface.  Operator paths
        always provide a positive bound and record each processed row, even
        when its object was already absent, so an empty early page cannot
        starve later cleanup work.
        """

        if limit is not None:
            self._positive_limit(limit)
        query = """SELECT session.id FROM upload_sessions AS session
                   LEFT JOIN upload_staging_cleanups AS cleanup
                     ON cleanup.upload_session_id=session.id
                   WHERE session.state IN ('verified','rejected','expired')
                     AND cleanup.upload_session_id IS NULL
                   ORDER BY session.finalized_at,session.id"""
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT %s"
            params = (limit,)
        rows = self._connection.execute(query, params).fetchall()
        return tuple(self.get_session(str(row[0])) for row in rows)

    def record_staging_cleanup(self, session_id: str) -> bool:
        """Record completed/idempotent deletion for one terminal staging key."""

        with self._connection.transaction():
            row = self._connection.execute(
                """INSERT INTO upload_staging_cleanups (upload_session_id)
                   SELECT id FROM upload_sessions
                   WHERE id=%s AND state IN ('verified','rejected','expired')
                   ON CONFLICT (upload_session_id) DO NOTHING
                   RETURNING upload_session_id""",
                (session_id,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _positive_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be positive")

    def _cas_state(
        self, session_id: str, version: int, before: UploadState, after: UploadState
    ) -> UploadSession:
        with self._connection.transaction():
            row = self._connection.execute(
                "UPDATE upload_sessions SET state=%s,version=version+1 WHERE id=%s AND version=%s AND state=%s AND clock_timestamp() < expires_at RETURNING id",
                (after.value, session_id, version, before.value),
            ).fetchone()
            if row is None:
                raise StateTransitionError("upload session CAS rejected")
        return self.get_session(session_id)

    def _locked(self, session_id: str) -> UploadSession:
        row = self._connection.execute(
            "SELECT id FROM upload_sessions WHERE id=%s FOR UPDATE", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("upload session not found: {0}".format(session_id))
        return self.get_session(session_id)
