"""Operator-owned production composition for the durable HTTP service.

This module deliberately contains deployment wiring only.  It does not select
models, execute workers, or turn operator configuration into a remote request
field.  PostgreSQL remains optional at import time so local/core users do not
need a database driver installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .application_adapters import (
    DurableAssetUploadWorkflow,
    PostgresApplicationRepository,
    PublishedArtifactCatalog,
)
from .artifact_storage import LocalAttemptArtifactStore
from .migrations import apply_postgres_migrations
from .observability import DurableHttpObserver, DurableMetrics, StructuredEventLogger
from .postgres_repository import PostgresDurableRepository
from .postgres_uploads import PostgresUploadRepository
from .publication import PostgresPublicationRepository
from .server import create_server
from .storage import LocalBlobStore
from .uploads import LocalStagingBlobStore, UploadVerifier, WaveMediaProbe
from .wiring import (
    StaticAuthorizationPolicy,
    StaticTokenAuthenticator,
    TokenGrant,
    build_application,
)

_ENV_DSN = "NANO_AURAL_DATABASE_DSN"
_ENV_CANONICAL_ROOT = "NANO_AURAL_CANONICAL_BLOB_ROOT"
_ENV_STAGING_ROOT = "NANO_AURAL_STAGING_BLOB_ROOT"
_ENV_ATTEMPT_ROOT = "NANO_AURAL_ATTEMPT_ARTIFACT_ROOT"
_ENV_TOKEN_GRANTS = "NANO_AURAL_TOKEN_GRANTS_JSON"


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("{0} must be a positive integer".format(name))
    return value


def _absolute_path(value: str, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} is required".format(name))
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("{0} must be an absolute operator-owned path".format(name))
    return path


def _grants_from_json(value: str) -> Tuple[TokenGrant, ...]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("NANO_AURAL_TOKEN_GRANTS_JSON must be a JSON array") from error
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("NANO_AURAL_TOKEN_GRANTS_JSON must be a non-empty JSON array")
    grants = []
    for item in parsed:
        if not isinstance(item, dict) or set(item) != {
            "token_sha256",
            "subject",
            "scopes",
            "namespaces",
        }:
            raise ValueError("each token grant must have token_sha256, subject, scopes, namespaces")
        scopes, namespaces = item["scopes"], item["namespaces"]
        if not isinstance(scopes, list) or not isinstance(namespaces, list):
            raise ValueError("token grant scopes and namespaces must be JSON arrays")
        grants.append(
            TokenGrant(
                item["token_sha256"],
                item["subject"],
                frozenset(scopes),
                frozenset(namespaces),
            )
        )
    return tuple(grants)


def _connect_postgres(database_dsn: str, connector: Optional[Callable[[str], Any]] = None) -> Any:
    if connector is None:
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError(
                "psycopg is required for the durable service; install the postgres runtime dependency"
            ) from error
        connect = psycopg.connect
    else:
        connect = connector
    return connect(database_dsn)


def _migration_dsn_from_environment(environ: Optional[Mapping[str, str]] = None) -> str:
    source = os.environ if environ is None else environ
    database_dsn = source.get(_ENV_DSN)
    if not database_dsn:
        raise ValueError("missing required operator configuration: {0}".format(_ENV_DSN))
    return database_dsn


@dataclass(frozen=True)
class DurableServiceConfig:
    """Non-secret service configuration supplied by the operator environment.

    Token grants contain only token digests.  The bearer token and the DSN are
    read at process start and are never logged or stored in source code.
    """

    database_dsn: str = field(repr=False)
    canonical_blob_root: Path
    staging_blob_root: Path
    attempt_artifact_root: Path
    token_grants: Tuple[TokenGrant, ...]
    host: str = "127.0.0.1"
    port: int = 8080
    max_body_bytes: int = 1024 * 1024
    max_upload_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.database_dsn, str) or not self.database_dsn.strip():
            raise ValueError("database_dsn must be non-empty")
        for value, name in (
            (self.canonical_blob_root, "canonical_blob_root"),
            (self.staging_blob_root, "staging_blob_root"),
            (self.attempt_artifact_root, "attempt_artifact_root"),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError("{0} must be an absolute path".format(name))
        grants = tuple(self.token_grants)
        if not grants or not all(isinstance(item, TokenGrant) for item in grants):
            raise ValueError("token_grants must contain at least one TokenGrant")
        object.__setattr__(self, "token_grants", grants)
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("host must be non-empty")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= 65535
        ):
            raise ValueError("port must be between 0 and 65535")
        _positive_int(self.max_body_bytes, "max_body_bytes")
        _positive_int(self.max_upload_bytes, "max_upload_bytes")

    @classmethod
    def from_environment(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "DurableServiceConfig":
        source = os.environ if environ is None else environ
        missing = [
            name
            for name in (
                _ENV_DSN,
                _ENV_CANONICAL_ROOT,
                _ENV_STAGING_ROOT,
                _ENV_ATTEMPT_ROOT,
                _ENV_TOKEN_GRANTS,
            )
            if not source.get(name)
        ]
        if missing:
            raise ValueError(
                "missing required operator configuration: {0}".format(", ".join(missing))
            )
        return cls(
            source[_ENV_DSN],
            _absolute_path(source[_ENV_CANONICAL_ROOT], _ENV_CANONICAL_ROOT),
            _absolute_path(source[_ENV_STAGING_ROOT], _ENV_STAGING_ROOT),
            _absolute_path(source[_ENV_ATTEMPT_ROOT], _ENV_ATTEMPT_ROOT),
            _grants_from_json(source[_ENV_TOKEN_GRANTS]),
            source.get("NANO_AURAL_API_HOST", "127.0.0.1"),
            int(source.get("NANO_AURAL_API_PORT", "8080")),
            int(source.get("NANO_AURAL_API_MAX_BODY_BYTES", str(1024 * 1024))),
            int(source.get("NANO_AURAL_API_MAX_UPLOAD_BYTES", str(1024 * 1024))),
        )


class DurableService:
    """Own the SQL connection and local stores used by one WSGI process."""

    def __init__(self, config: DurableServiceConfig, connection: Any) -> None:
        self.config = config
        self._connection = connection
        self._closed = False
        try:
            # Repository commands delimit their own transactions.  The WSGI
            # read paths intentionally issue simple SELECTs, so a
            # service-owned psycopg connection must not retain one implicit
            # transaction across requests and turn a later command into a
            # nested savepoint.
            if hasattr(connection, "autocommit"):
                connection.autocommit = True
            self.canonical_store = LocalBlobStore(config.canonical_blob_root)
            self.staging_store = LocalStagingBlobStore(config.staging_blob_root)
            self.attempt_store = LocalAttemptArtifactStore(config.attempt_artifact_root)
            self.durable_repository = PostgresDurableRepository(connection)
            self.publication_repository = PostgresPublicationRepository(connection)
            self.repository = PostgresApplicationRepository(connection, self.durable_repository)
            self.artifacts = PublishedArtifactCatalog(
                self.publication_repository, self.canonical_store
            )
            self.upload_repository = PostgresUploadRepository(connection)
            verifier = UploadVerifier(
                self.upload_repository,
                self.staging_store,
                self.canonical_store,
                WaveMediaProbe(),
                max_upload_bytes=config.max_upload_bytes,
            )
            self.uploads = DurableAssetUploadWorkflow(
                self.upload_repository,
                self.staging_store,
                verifier,
                max_upload_bytes=config.max_upload_bytes,
            )
            self.authenticator = StaticTokenAuthenticator(config.token_grants)
            self.authorization = StaticAuthorizationPolicy(config.token_grants)
            self.api = build_application(self)
            self.metrics = DurableMetrics()
            self.http_observer = DurableHttpObserver(
                self.metrics,
                StructuredEventLogger(sys.stderr),
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def connect(
        cls,
        config: DurableServiceConfig,
        connector: Optional[Callable[[str], Any]] = None,
    ) -> "DurableService":
        connection = _connect_postgres(config.database_dsn, connector)
        try:
            return cls(config, connection)
        except BaseException:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            raise

    def create_server(self):  # type: ignore[no-untyped-def]
        if self._closed:
            raise RuntimeError("durable service is closed")
        return create_server(
            self.api,
            host=self.config.host,
            port=self.config.port,
            max_body_bytes=self.config.max_body_bytes,
            observer=self.http_observer,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in (
            getattr(self, "attempt_store", None),
            getattr(self, "staging_store", None),
            getattr(self, "canonical_store", None),
            getattr(self, "_connection", None),
        ):
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "DurableService":
        return self

    def __exit__(self, *_unused: object) -> None:
        self.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the durable HTTP process from operator-owned environment config."""

    parser = argparse.ArgumentParser(
        description="Start the nanoAural durable HTTP API service.",
        epilog=(
            "Serving requires: NANO_AURAL_DATABASE_DSN, "
            "NANO_AURAL_CANONICAL_BLOB_ROOT, NANO_AURAL_STAGING_BLOB_ROOT, "
            "NANO_AURAL_ATTEMPT_ARTIFACT_ROOT, and NANO_AURAL_TOKEN_GRANTS_JSON; "
            "--migrate-only requires only NANO_AURAL_DATABASE_DSN."
        ),
    )
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="apply pending PostgreSQL migrations and exit",
    )
    options = parser.parse_args(argv)
    if options.migrate_only:
        try:
            connection = _connect_postgres(_migration_dsn_from_environment())
        except ValueError:
            sys.stderr.write(
                "nano-aural durable migration configuration failed; check NANO_AURAL_DATABASE_DSN\n"
            )
            return 2
        except RuntimeError:
            sys.stderr.write("nano-aural durable migration failed; check runtime dependencies\n")
            return 2
        except Exception:
            sys.stderr.write(
                "nano-aural durable migration failed: unable to connect; check PostgreSQL availability\n"
            )
            return 2
        failed = False
        try:
            apply_postgres_migrations(connection)
        except Exception:
            failed = True
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                failed = True
        if failed:
            # Migration and close exceptions may embed the DSN, SQL, path, or
            # credentials, including in chained causes.  Consume the entire
            # exception chain at this public process boundary.
            sys.stderr.write("nano-aural durable migration failed; check operator logs\n")
            return 2
        return 0
    try:
        config = DurableServiceConfig.from_environment()
        service = DurableService.connect(config)
    except ValueError:
        sys.stderr.write(
            "nano-aural durable service configuration failed; "
            "check required NANO_AURAL_* settings\n"
        )
        return 2
    except RuntimeError:
        sys.stderr.write(
            "nano-aural durable service configuration failed; check runtime dependencies\n"
        )
        return 2
    except Exception:
        sys.stderr.write(
            "nano-aural durable service configuration failed: unable to connect; check PostgreSQL availability\n"
        )
        return 2
    try:
        with service:
            with service.create_server() as server:
                server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Server creation, serving, and resource close errors may contain a
        # DSN, URL, path, or credential in their message/chained causes.
        sys.stderr.write("nano-aural durable service failed; check operator logs\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
