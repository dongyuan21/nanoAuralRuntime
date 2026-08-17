"""Operator CLI for verifying or explicitly adopting migration checksums."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .migrations import (
    MigrationIntegrityError,
    adopt_legacy_postgres_migration_checksums,
    grant_postgres_runtime_role,
    verify_postgres_migrations,
)

_ENV_DSN = "NANO_AURAL_DATABASE_DSN"
_MANIFEST_SCHEMA = "nano-aural-migration-checksums/v1"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_MIGRATIONS = 100


def _open_manifest(path_value: str) -> Mapping[str, str]:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError("manifest path must be absolute")
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) & 0o077
        ):
            raise ValueError("manifest must be an owner-only regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(16 * 1024, _MAX_MANIFEST_BYTES - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_MANIFEST_BYTES:
                raise ValueError("manifest exceeds bounded size")
        if total != information.st_size:
            raise ValueError("manifest changed while being read")
    finally:
        os.close(descriptor)
    data = b"".join(chunks)

    def no_duplicates(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("manifest contains duplicate keys")
            value[key] = item
        return value

    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("manifest must be strict UTF-8 JSON") from error
    if not isinstance(parsed, dict) or set(parsed) != {"schema", "migrations"}:
        raise ValueError("manifest shape is invalid")
    entries = parsed["migrations"]
    if parsed["schema"] != _MANIFEST_SCHEMA or not isinstance(entries, list):
        raise ValueError("manifest schema is invalid")
    if not entries or len(entries) > _MAX_MIGRATIONS:
        raise ValueError("manifest migration count is invalid")
    result: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"filename", "sha256"}:
            raise ValueError("manifest migration entry is invalid")
        filename, digest = item["filename"], item["sha256"]
        if not isinstance(filename, str) or not isinstance(digest, str) or filename in result:
            raise ValueError("manifest migration identity is invalid")
        result[filename] = digest
    return result


def _connect(dsn: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("POSTGRES_DRIVER_UNAVAILABLE") from error
    return psycopg.connect(dsn)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify or explicitly adopt nanoAural PostgreSQL migration checksums."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify", action="store_true", help="verify the sealed migration ledger")
    action.add_argument(
        "--adopt-checksums",
        metavar="ABSOLUTE_JSON",
        help="seal a legacy filename-only ledger using an owner-only trusted manifest",
    )
    action.add_argument(
        "--grant-runtime-role",
        choices=("nano_aural_runtime",),
        help="grant the fixed Compose runtime role least-privilege application access",
    )
    options = parser.parse_args(argv)
    dsn = os.environ.get(_ENV_DSN)
    if not dsn:
        sys.stderr.write("MIGRATION_ADMIN_CONFIG_INVALID\n")
        return 2
    trusted: Optional[Mapping[str, str]] = None
    if options.adopt_checksums is not None:
        try:
            trusted = _open_manifest(options.adopt_checksums)
        except (OSError, ValueError):
            sys.stderr.write("MIGRATION_ADOPTION_MANIFEST_INVALID\n")
            return 2
    try:
        connection = _connect(dsn)
    except Exception:
        sys.stderr.write("MIGRATION_ADMIN_DATABASE_UNAVAILABLE\n")
        return 2
    error_code: Optional[str] = None
    try:
        if options.verify:
            verify_postgres_migrations(connection)
        elif options.grant_runtime_role is not None:
            grant_postgres_runtime_role(connection, options.grant_runtime_role)
        else:
            assert trusted is not None
            adopt_legacy_postgres_migration_checksums(connection, trusted)
    except MigrationIntegrityError as error:
        error_code = error.code
    except Exception:
        error_code = "MIGRATION_ADMIN_OPERATION_FAILED"
    close = getattr(connection, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            error_code = "MIGRATION_ADMIN_CLOSE_FAILED"
    if error_code is not None:
        sys.stderr.write(error_code + "\n")
        return 2
    if options.verify:
        outcome = "MIGRATION_LEDGER_VERIFIED"
    elif options.grant_runtime_role is not None:
        outcome = "MIGRATION_RUNTIME_ROLE_GRANTED"
    else:
        outcome = "MIGRATION_LEDGER_ADOPTED"
    sys.stdout.write(outcome + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
