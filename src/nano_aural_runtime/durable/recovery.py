"""Bounded operator recovery commands for the Phase 3E reference environment."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .artifact_publication import ArtifactOrphanSweeper
from .artifact_storage import LocalAttemptArtifactStore
from .observability import DurableMetrics, StructuredEventLogger
from .postgres_uploads import PostgresUploadRepository
from .publication import PostgresPublicationRepository
from .queue import PostgresLeaseQueue
from .uploads import LocalStagingBlobStore


def _connect(database_dsn: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "psycopg is required for durable recovery commands; install the postgres runtime dependency"
        ) from error
    return psycopg.connect(database_dsn, autocommit=True)


def _environment(name: str, environ: Optional[Mapping[str, str]] = None) -> str:
    source = os.environ if environ is None else environ
    value = source.get(name)
    if not value:
        raise ValueError("missing required operator configuration: {0}".format(name))
    return value


def _root(name: str) -> Path:
    path = Path(_environment(name))
    if not path.is_absolute():
        raise ValueError("{0} must be an absolute operator-owned path".format(name))
    return path


def _cutoff(seconds: int) -> datetime:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 300:
        raise ValueError("grace-seconds must be at least 300")
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _dry_run_attempts(connection: Any, root: Path, before: datetime, limit: int) -> int:
    repository = PostgresPublicationRepository(connection)
    store = LocalAttemptArtifactStore(root)
    try:
        candidates = {item.publication_id for item in repository.cleanup_candidates(before, limit)}
        remaining = max(0, limit - len(candidates))
        orphan_keys = repository.orphan_keys_before(before, remaining) if remaining else ()
        for key in orphan_keys:
            candidates.add(key)
        if len(candidates) < limit:
            page = store.inventory_page_before(before, limit - len(candidates), "orphan_dry_run")
            inventory = page.objects
            known = set(repository.known_attempt_keys([item.storage_key for item in inventory]))
            candidates.update(
                item.storage_key for item in inventory if item.storage_key not in known
            )
        return min(limit, len(candidates))
    finally:
        store.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run bounded durable recovery actions using database authority.",
        epilog="Canonical blobs are intentionally outside every deletion command.",
    )
    parser.add_argument("--limit", type=int, default=100, help="maximum rows/objects per run")
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=600,
        help="minimum age for attempt-object inspection (at least 300)",
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--reap-expired", action="store_true", help="reap expired DB leases")
    actions.add_argument(
        "--expire-uploads",
        action="store_true",
        help="expire DB-clock-overdue uploads and delete terminal staging bytes",
    )
    actions.add_argument(
        "--attempt-orphans-dry-run",
        action="store_true",
        help="count age-bounded attempt-object cleanup candidates without mutation",
    )
    actions.add_argument(
        "--sweep-attempt-orphans",
        action="store_true",
        help="delete only age-bounded, DB-authorized attempt objects",
    )
    options = parser.parse_args(argv)
    if options.limit < 1:
        parser.error("--limit must be positive")
    try:
        before = _cutoff(options.grace_seconds)
        connection = _connect(_environment("NANO_AURAL_DATABASE_DSN"))
    except ValueError:
        sys.stderr.write(
            "durable recovery configuration failed; check required NANO_AURAL_* settings\n"
        )
        return 2
    except RuntimeError:
        sys.stderr.write("durable recovery configuration failed; check runtime dependencies\n")
        return 2
    except Exception:
        sys.stderr.write("durable recovery could not connect to PostgreSQL\n")
        return 2
    events = StructuredEventLogger(sys.stderr)
    metrics = DurableMetrics()
    count = 0
    failed = False
    try:
        if options.reap_expired:
            count = PostgresLeaseQueue(connection).reap_expired(limit=options.limit)
            if count:
                metrics.increment(
                    "nano_aural_lease_events_total",
                    {"event": "reaped", "outcome": "success"},
                    count,
                )
            events.emit(component="queue", outcome="success", numeric={"count": count})
        elif options.expire_uploads:
            repository = PostgresUploadRepository(connection)
            staging = LocalStagingBlobStore(_root("NANO_AURAL_STAGING_BLOB_ROOT"))
            try:
                expired = repository.expire_batch(options.limit)
                count = len(expired)
                remaining = options.limit - count
                candidates = repository.terminal_staging_candidates(remaining) if remaining else ()
                for session in candidates:
                    if staging.exists(session.staging_key):
                        staging.delete(session.staging_key)
                    repository.record_staging_cleanup(session.session_id)
                    count += 1
            finally:
                staging.close()
            events.emit(component="worker", outcome="success", numeric={"count": count})
        elif options.attempt_orphans_dry_run:
            count = _dry_run_attempts(
                connection,
                _root("NANO_AURAL_ATTEMPT_ARTIFACT_ROOT"),
                before,
                options.limit,
            )
            if count:
                metrics.increment(
                    "nano_aural_orphan_actions_total",
                    {"action": "retain", "outcome": "success"},
                    count,
                )
            events.emit(component="orphan_sweeper", outcome="success", numeric={"count": count})
        else:
            store = LocalAttemptArtifactStore(_root("NANO_AURAL_ATTEMPT_ARTIFACT_ROOT"))
            try:
                count = ArtifactOrphanSweeper(
                    PostgresPublicationRepository(connection), store
                ).sweep(before, options.limit)
            finally:
                store.close()
            if count:
                metrics.increment(
                    "nano_aural_orphan_actions_total",
                    {"action": "delete", "outcome": "success"},
                    count,
                )
            events.emit(component="orphan_sweeper", outcome="success", numeric={"count": count})
    except Exception:
        failed = True
        component = (
            "orphan_sweeper"
            if options.attempt_orphans_dry_run or options.sweep_attempt_orphans
            else "worker"
        )
        try:
            events.emit(component=component, outcome="failed", reason_code="database")
        except Exception:
            # The diagnostic sink is untrusted at this process boundary.  Its
            # own exception must not replace the original failure or expose a
            # chained message containing operator configuration.
            pass
    try:
        connection.close()
    except Exception:
        failed = True
    if failed:
        sys.stderr.write("durable recovery failed; check operator logs\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
