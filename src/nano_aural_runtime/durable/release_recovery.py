"""Bounded PostgreSQL plus canonical-blob release recovery sets.

Recovery sets intentionally contain no DSN, service-file path, namespace,
request, token, or host metadata.  PostgreSQL tools are invoked through a
named libpq service and their stderr is never relayed to callers.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional, Protocol, Sequence, Tuple
from uuid import uuid4

from .migrations import _CHECK_EXPRESSION, _LEDGER_FUNCTION_SOURCE, _migration_sources
from .storage import LocalBlobStore

_SCHEMA = "nano-aural-recovery-set/v1"
_SERVICE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
_HEX_DIR = re.compile(r"^[0-9a-f]{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_OPERATION_SCHEMA = "nano-aural-recovery-operation/v1"
_MAX_SERVICE_FILE_BYTES = 64 * 1024


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_EXPECTED_MIGRATION_VALUES_SQL = ",\n  ".join(
    "(" + _sql_literal(source.filename) + "," + _sql_literal(source.sha256) + f",{ordinal})"
    for ordinal, source in enumerate(_migration_sources(None), start=1)
)


_LEDGER_AUTHORITY_SQL = f"""WITH ledger AS (
  SELECT r.oid,r.relowner,r.relacl
  FROM pg_catalog.pg_class AS r
  JOIN pg_catalog.pg_namespace AS n ON n.oid=r.relnamespace
  WHERE n.nspname='public' AND r.relname='nano_aural_schema_migrations'
    AND r.relkind IN ('r','p')
), expected(filename,sha256,ordinal) AS (
  VALUES {_EXPECTED_MIGRATION_VALUES_SQL}
), observed(filename,sha256,ordinal) AS (
  SELECT m.filename,m.sha256,
         (pg_catalog.row_number() OVER (ORDER BY m.filename))::pg_catalog.int4
  FROM public.nano_aural_schema_migrations AS m
)
SELECT CASE WHEN
  (SELECT pg_catalog.count(*) FROM ledger)=1
  AND EXISTS (
    SELECT 1 FROM ledger AS l
    JOIN pg_catalog.pg_attribute AS a ON a.attrelid=l.oid
    WHERE a.attname='sha256' AND a.attnum>0 AND NOT a.attisdropped
      AND a.attnotnull AND a.atttypid='pg_catalog.text'::pg_catalog.regtype
  )
  AND (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint AS c, ledger AS l
       WHERE c.conrelid=l.oid
         AND c.conname='nano_aural_schema_migrations_sha256_format')=1
  AND EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS c, ledger AS l
    WHERE c.conrelid=l.oid
      AND c.conname='nano_aural_schema_migrations_sha256_format'
      AND c.contype='c' AND c.convalidated
      AND pg_catalog.pg_get_expr(c.conbin,c.conrelid)={_sql_literal(_CHECK_EXPRESSION)}
  )
  AND NOT EXISTS (
    SELECT 1 FROM ledger AS l
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(l.relacl,pg_catalog.acldefault('r',l.relowner))
    ) AS permission
    WHERE permission.privilege_type IN ('INSERT','UPDATE','DELETE')
      AND permission.grantee<>l.relowner
  )
  AND NOT EXISTS (
    SELECT 1 FROM ledger AS l
    JOIN pg_catalog.pg_attribute AS protected_column
      ON protected_column.attrelid=l.oid
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      protected_column.attacl
    ) AS permission
    WHERE protected_column.attnum>0 AND NOT protected_column.attisdropped
      AND protected_column.attacl IS NOT NULL
      AND permission.privilege_type IN ('INSERT','UPDATE','DELETE')
      AND permission.grantee<>l.relowner
  )
  AND (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger AS t, ledger AS l
       WHERE t.tgrelid=l.oid AND NOT t.tgisinternal
         AND t.tgname='nano_aural_schema_migrations_guard')=1
  AND EXISTS (
    SELECT 1
    FROM ledger AS l
    JOIN pg_catalog.pg_trigger AS t ON t.tgrelid=l.oid
    JOIN pg_catalog.pg_proc AS p ON p.oid=t.tgfoid
    JOIN pg_catalog.pg_namespace AS np ON np.oid=p.pronamespace
    WHERE NOT t.tgisinternal
      AND t.tgname='nano_aural_schema_migrations_guard'
      AND t.tgenabled IN ('O','A') AND t.tgtype=31
      AND np.nspname='public'
      AND p.proname='nano_aural_guard_schema_migration_ledger'
      AND p.pronargs=0 AND NOT p.prosecdef AND NOT p.proleakproof
      AND p.proowner=l.relowner
      AND pg_catalog.encode(pg_catalog.convert_to(p.prosrc,'UTF8'),'hex')=
          {_sql_literal(_LEDGER_FUNCTION_SOURCE.encode("utf-8").hex())}
  )
  AND (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_proc AS p
       JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
       WHERE n.nspname='public'
         AND p.proname='nano_aural_guard_schema_migration_ledger')=1
  AND (SELECT pg_catalog.count(*) FROM public.nano_aural_schema_migrations)>0
  AND NOT EXISTS (
    SELECT 1 FROM public.nano_aural_schema_migrations AS m
    WHERE m.sha256 IS NULL OR m.sha256 !~ '^[0-9a-f]{{64}}$'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM expected AS e
    FULL OUTER JOIN observed AS o ON o.ordinal=e.ordinal
    WHERE e.filename IS DISTINCT FROM o.filename
       OR e.sha256 IS DISTINCT FROM o.sha256
  )
THEN 1 ELSE 0 END"""

_DATABASE_IDENTITY_SQL = """SELECT
  s.system_identifier::pg_catalog.text || ':' || d.oid::pg_catalog.text || ':' ||
  pg_catalog.current_database()
FROM pg_catalog.pg_control_system() AS s
JOIN pg_catalog.pg_database AS d ON d.datname=pg_catalog.current_database()"""


class RecoverySetError(RuntimeError):
    """A recovery set violated an integrity or safety contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryEnvironmentUnavailable(RecoverySetError):
    """The required external PostgreSQL client toolchain is unavailable."""


@dataclass(frozen=True)
class RecoveryLimits:
    max_objects: int = 100_000
    max_single_blob_bytes: int = 1 << 36
    max_total_blob_bytes: int = 1 << 40
    max_database_dump_bytes: int = 1 << 40
    max_temporary_bytes: int = 2 << 40

    def __post_init__(self) -> None:
        for name in (
            "max_objects",
            "max_single_blob_bytes",
            "max_total_blob_bytes",
            "max_database_dump_bytes",
            "max_temporary_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("recovery limits must be positive integers")


_DEFAULT_LIMITS = RecoveryLimits()


def _no_progress(_stage: str) -> None:
    return None


@dataclass(frozen=True)
class RecoveryObject:
    storage_key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RecoveryEvidence:
    object_count: int
    blob_bytes: int
    database_dump_bytes: int


@dataclass(frozen=True)
class ValidatedRecoverySet:
    manifest_sha256: str
    dump_sha256: str
    dump_size_bytes: int
    source_database_identity: str
    objects: Tuple[RecoveryObject, ...]


class PostgresRecoveryTools(Protocol):
    def database_identity(self) -> str: ...

    def dump(self, destination: Path, max_bytes: int) -> Tuple[str, int]: ...

    def target_database_is_empty(self) -> bool: ...

    def target_database_has_sealed_migration_ledger(self) -> bool: ...

    def restore(self, source: Path, expected_sha256: str, max_bytes: int) -> None: ...


def _private_absolute_directory(path: Path, *, existing: bool) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise RecoverySetError("RECOVERY_PATH_INVALID")
    checked = value if existing else value.parent
    try:
        information = os.lstat(str(checked))
        resolved = checked.resolve(strict=True)
    except OSError as error:
        raise RecoverySetError("RECOVERY_PATH_INVALID") from error
    if (
        not stat.S_ISDIR(information.st_mode)
        or stat.S_ISLNK(information.st_mode)
        or resolved != checked
        or information.st_uid != os.getuid()
        or stat.S_IMODE(information.st_mode) != 0o700
    ):
        raise RecoverySetError("RECOVERY_PATH_NOT_PRIVATE")
    if existing:
        return value
    try:
        os.lstat(str(value))
    except FileNotFoundError:
        return value
    except OSError as error:
        raise RecoverySetError("RECOVERY_PATH_INVALID") from error
    raise RecoverySetError("RECOVERY_TARGET_EXISTS")


def _private_absolute_target(path: Path) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise RecoverySetError("RECOVERY_PATH_INVALID")
    _private_absolute_directory(value.parent, existing=True)
    return value


def _require_disjoint(first: Path, second: Path) -> None:
    try:
        first.relative_to(second)
    except ValueError:
        pass
    else:
        raise RecoverySetError("RECOVERY_PATHS_OVERLAP")
    try:
        second.relative_to(first)
    except ValueError:
        return
    raise RecoverySetError("RECOVERY_PATHS_OVERLAP")


def _regular_file(path: Path) -> os.stat_result:
    try:
        information = os.lstat(str(path))
    except OSError as error:
        raise RecoverySetError("RECOVERY_FILE_INVALID") from error
    if (
        stat.S_ISLNK(information.st_mode)
        or not stat.S_ISREG(information.st_mode)
        or information.st_uid != os.getuid()
        or stat.S_IMODE(information.st_mode) != 0o600
    ):
        raise RecoverySetError("RECOVERY_FILE_INVALID")
    return information


def _hash_file(path: Path, max_bytes: int) -> Tuple[str, int]:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    size = 0
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != 0o600
        ):
            raise RecoverySetError("RECOVERY_FILE_INVALID")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest(), size


def _canonical_keys(root: Path, limits: RecoveryLimits) -> Tuple[str, ...]:
    keys = []
    observed_directories = set()
    try:
        for directory, directory_names, filenames in os.walk(str(root), followlinks=False):
            current = Path(directory)
            relative = current.relative_to(root)
            parts = relative.parts
            if parts == ():
                allowed_directories = {"blobs"}
            elif parts == ("blobs",):
                allowed_directories = {"sha256"}
            elif len(parts) in (2, 3) and parts[:2] == ("blobs", "sha256"):
                allowed_directories = None
            elif len(parts) == 4 and parts[:2] == ("blobs", "sha256"):
                allowed_directories = set()
            else:
                raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
            for name in tuple(directory_names):
                child = current / name
                information = os.lstat(str(child))
                if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                if information.st_uid != os.getuid() or stat.S_IMODE(information.st_mode) != 0o700:
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                if allowed_directories is not None and name not in allowed_directories:
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                if len(parts) in (2, 3) and not _HEX_DIR.fullmatch(name):
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                observed_directories.add(child.relative_to(root).as_posix())
            for name in filenames:
                child = current / name
                information = os.lstat(str(child))
                if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                if information.st_uid != os.getuid() or stat.S_IMODE(information.st_mode) != 0o600:
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                key = child.relative_to(root).as_posix()
                if len(parts) != 4 or not _SHA256.fullmatch(name):
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                try:
                    canonical = LocalBlobStore.canonical_key(name)
                except ValueError as error:
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID") from error
                if key != canonical:
                    raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
                keys.append(key)
                if len(keys) > limits.max_objects:
                    raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
    except OSError as error:
        raise RecoverySetError("RECOVERY_CANONICAL_READ_FAILED") from error
    expected_directories = set()
    for key in keys:
        parts = key.split("/")[:-1]
        expected_directories.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    if observed_directories != expected_directories:
        raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
    return tuple(sorted(keys))


def _copy_stream(source: BinaryIO, destination: Path, max_bytes: int) -> Tuple[str, int]:
    missing = []
    parent = destination.parent
    while True:
        try:
            information = os.lstat(str(parent))
        except FileNotFoundError:
            missing.append(parent)
            parent = parent.parent
            continue
        if (
            stat.S_ISLNK(information.st_mode)
            or not stat.S_ISDIR(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != 0o700
        ):
            raise RecoverySetError("RECOVERY_CANONICAL_NAMESPACE_INVALID")
        break
    for directory in reversed(missing):
        os.mkdir(str(directory), 0o700)
    descriptor = os.open(
        str(destination),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            while True:
                chunk = source.read(1024 * 1024)
                if not isinstance(chunk, bytes):
                    raise RecoverySetError("RECOVERY_STREAM_INVALID")
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest(), size


def _hash_stream(source: BinaryIO, max_bytes: int) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not isinstance(chunk, bytes):
            raise RecoverySetError("RECOVERY_STREAM_INVALID")
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
        digest.update(chunk)
    return digest.hexdigest(), size


def _fsync_tree(root: Path) -> None:
    """Durably record every already-fsynced file's directory entry."""

    directories = []
    try:
        for directory, directory_names, filenames in os.walk(str(root), topdown=False):
            current = Path(directory)
            for name in (*directory_names, *filenames):
                information = os.lstat(str(current / name))
                if stat.S_ISLNK(information.st_mode):
                    raise RecoverySetError("RECOVERY_SET_INVALID")
            directories.append(current)
        for directory in directories:
            descriptor = os.open(
                str(directory),
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise RecoverySetError("RECOVERY_DURABILITY_SYNC_FAILED") from error


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    temporary = path.parent / ("." + path.name + ".incomplete-" + uuid4().hex)
    descriptor = os.open(
        str(temporary),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_bounded_json(path: Path, max_bytes: int) -> object:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != 0o600
            or information.st_size > max_bytes
        ):
            raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
        if total != information.st_size:
            raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
    finally:
        os.close(descriptor)
    data = b"".join(chunks)

    def no_duplicates(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoverySetError("RECOVERY_MANIFEST_INVALID") from error


def _read_manifest(
    path: Path, limits: RecoveryLimits
) -> Tuple[str, int, str, Tuple[RecoveryObject, ...]]:
    try:
        parsed = _read_bounded_json(path, _MAX_MANIFEST_BYTES)
    except OSError as error:
        raise RecoverySetError("RECOVERY_MANIFEST_INVALID") from error
    if not isinstance(parsed, dict) or set(parsed) != {"schema", "database", "canonical"}:
        raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
    database, canonical = parsed["database"], parsed["canonical"]
    if (
        parsed["schema"] != _SCHEMA
        or not isinstance(database, dict)
        or set(database) != {"format", "sha256", "size_bytes", "source_database_identity"}
        or database["format"] != "postgres-custom"
        or not isinstance(canonical, dict)
        or set(canonical) != {"objects", "total_bytes"}
        or not isinstance(canonical["objects"], list)
    ):
        raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
    dump_digest, dump_size = database["sha256"], database["size_bytes"]
    source_identity = database["source_database_identity"]
    if (
        not isinstance(dump_digest, str)
        or not _SHA256.fullmatch(dump_digest)
        or isinstance(dump_size, bool)
        or not isinstance(dump_size, int)
        or dump_size < 1
        or dump_size > limits.max_database_dump_bytes
        or not isinstance(source_identity, str)
        or not _SHA256.fullmatch(source_identity)
    ):
        raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
    objects = []
    seen = set()
    total = 0
    if len(canonical["objects"]) > limits.max_objects:
        raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
    for item in canonical["objects"]:
        if not isinstance(item, dict) or set(item) != {"storage_key", "sha256", "size_bytes"}:
            raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
        key, digest, size = item["storage_key"], item["sha256"], item["size_bytes"]
        if (
            not isinstance(key, str)
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or key != LocalBlobStore.canonical_key(digest)
            or key in seen
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
        seen.add(key)
        total += size
        if size > limits.max_single_blob_bytes or total > limits.max_total_blob_bytes:
            raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
        objects.append(RecoveryObject(key, digest, size))
    if (
        isinstance(canonical["total_bytes"], bool)
        or canonical["total_bytes"] != total
        or tuple(item.storage_key for item in objects) != tuple(sorted(seen))
    ):
        raise RecoverySetError("RECOVERY_MANIFEST_INVALID")
    return dump_digest, dump_size, source_identity, tuple(objects)


def _validate_recovery_set(source: Path, limits: RecoveryLimits) -> ValidatedRecoverySet:
    try:
        top_level = {entry.name for entry in os.scandir(str(source))}
    except OSError as error:
        raise RecoverySetError("RECOVERY_SET_INVALID") from error
    if top_level != {"database.dump", "manifest.json", "canonical"}:
        raise RecoverySetError("RECOVERY_SET_INVALID")
    dump_digest, dump_size, source_identity, objects = _read_manifest(
        source / "manifest.json", limits
    )
    observed_dump_digest, observed_dump_size = _hash_file(
        source / "database.dump", limits.max_database_dump_bytes
    )
    if (observed_dump_digest, observed_dump_size) != (dump_digest, dump_size):
        raise RecoverySetError("RECOVERY_DATABASE_INTEGRITY_FAILED")
    source_canonical = source / "canonical"
    _private_absolute_directory(source_canonical, existing=True)
    keys = _canonical_keys(source_canonical, limits)
    if keys != tuple(item.storage_key for item in objects):
        raise RecoverySetError("RECOVERY_CANONICAL_INVENTORY_MISMATCH")
    source_store = LocalBlobStore(source_canonical)
    try:
        try:
            for item in objects:
                with source_store.open_reader(item.storage_key) as reader:
                    observed = _hash_stream(reader, item.size_bytes)
                if observed != (item.sha256, item.size_bytes):
                    raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED")
        except (OSError, RuntimeError, ValueError) as error:
            raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED") from error
    finally:
        source_store.close()
    manifest_digest, _manifest_size = _hash_file(source / "manifest.json", _MAX_MANIFEST_BYTES)
    return ValidatedRecoverySet(
        manifest_digest,
        dump_digest,
        dump_size,
        source_identity,
        objects,
    )


def _validate_canonical_root(
    root: Path, objects: Sequence[RecoveryObject], limits: RecoveryLimits
) -> None:
    keys = _canonical_keys(root, limits)
    if keys != tuple(item.storage_key for item in objects):
        raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED")
    store = LocalBlobStore(root)
    try:
        try:
            for item in objects:
                with store.open_reader(item.storage_key) as reader:
                    observed = _hash_stream(reader, item.size_bytes)
                if observed != (item.sha256, item.size_bytes):
                    raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED")
        except (OSError, RuntimeError, ValueError) as error:
            raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED") from error
    finally:
        store.close()


def _operation_paths(target: Path, action: str) -> Tuple[Path, Path]:
    if target.name in ("", ".", ".."):
        raise RecoverySetError("RECOVERY_PATH_INVALID")
    return (
        target.parent / ("." + target.name + ".nano-" + action + "-stage"),
        target.parent / ("." + target.name + ".nano-" + action + "-state.json"),
    )


def _write_operation_state(
    path: Path,
    action: str,
    phase: str,
    database_identity: str,
    manifest_sha256: Optional[str] = None,
) -> None:
    if not _SHA256.fullmatch(database_identity):
        raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_INVALID")
    value: dict[str, object] = {
        "schema": _OPERATION_SCHEMA,
        "action": action,
        "phase": phase,
        "database_identity": database_identity,
    }
    if manifest_sha256 is not None:
        value["manifest_sha256"] = manifest_sha256
    _atomic_json(path, value)


def _read_operation_state(path: Path, action: str) -> Mapping[str, object]:
    try:
        information = os.lstat(str(path))
    except OSError as error:
        raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID") from error
    if (
        stat.S_ISLNK(information.st_mode)
        or not stat.S_ISREG(information.st_mode)
        or information.st_uid != os.getuid()
        or stat.S_IMODE(information.st_mode) != 0o600
    ):
        raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
    value = _read_bounded_json(path, 4096)
    if (
        not isinstance(value, dict)
        or set(value)
        not in (
            {"schema", "action", "phase", "database_identity"},
            {"schema", "action", "phase", "database_identity", "manifest_sha256"},
        )
        or value.get("schema") != _OPERATION_SCHEMA
        or value.get("action") != action
        or not isinstance(value.get("phase"), str)
    ):
        raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
    digest = value.get("manifest_sha256")
    if digest is not None and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
    database_identity = value.get("database_identity")
    if not isinstance(database_identity, str) or not _SHA256.fullmatch(database_identity):
        raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
    return value


def _unlink_durable(path: Path) -> None:
    path.unlink()
    directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _publish_directory(stage: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes, target_bytes = os.fsencode(stage), os.fsencode(target)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, target_bytes, 0x00000001)
    else:
        raise RecoveryEnvironmentUnavailable("RECOVERY_NOREPLACE_RENAME_UNAVAILABLE")
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in (errno.EEXIST, errno.ENOTEMPTY):
            raise RecoverySetError("RECOVERY_TARGET_EXISTS")
        raise RecoverySetError("RECOVERY_PUBLICATION_FAILED") from OSError(error_number, "rename")
    directory_fd = os.open(str(target.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _remove_owned_stage(stage: Path) -> None:
    try:
        information = os.lstat(str(stage))
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(information.st_mode)
        or not stat.S_ISDIR(information.st_mode)
        or information.st_uid != os.getuid()
        or stat.S_IMODE(information.st_mode) != 0o700
        or stage.resolve(strict=True) != stage
    ):
        raise RecoverySetError("RECOVERY_OPERATION_STAGE_INVALID")
    shutil.rmtree(stage)
    directory_fd = os.open(str(stage.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _require_database_identity(tools: PostgresRecoveryTools, expected: str) -> None:
    observed = tools.database_identity()
    if observed != expected:
        raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_CHANGED")


class SubprocessPostgresRecoveryTools:
    """Fixed, descriptor-bound subprocess boundary for PostgreSQL clients."""

    def __init__(
        self,
        postgres_bin_root: Path,
        pg_service_file: Path,
        service_name: str,
        *,
        timeout_seconds: int = 900,
    ) -> None:
        if not _SERVICE.fullmatch(service_name):
            raise RecoverySetError("RECOVERY_SERVICE_INVALID")
        if isinstance(timeout_seconds, bool) or timeout_seconds < 1 or timeout_seconds > 3600:
            raise ValueError("timeout_seconds must be between 1 and 3600")
        binary_root = Path(postgres_bin_root)
        service_file = Path(pg_service_file)
        if not binary_root.is_absolute() or not service_file.is_absolute():
            raise RecoverySetError("RECOVERY_TOOLCHAIN_INVALID")
        _private_absolute_directory(service_file.parent, existing=True)
        try:
            binary_root_info = os.lstat(str(binary_root))
        except OSError as error:
            raise RecoveryEnvironmentUnavailable("RECOVERY_TOOLCHAIN_UNAVAILABLE") from error
        if (
            stat.S_ISLNK(binary_root_info.st_mode)
            or not stat.S_ISDIR(binary_root_info.st_mode)
            or binary_root.resolve(strict=True) != binary_root
            or binary_root_info.st_uid not in (0, os.getuid())
            or stat.S_IMODE(binary_root_info.st_mode) & 0o022
        ):
            raise RecoverySetError("RECOVERY_TOOLCHAIN_INVALID")
        descriptors: dict[str, int] = {}
        binary_paths: dict[str, Path] = {}
        binary_identities: dict[str, Tuple[int, int, int, int, int, int]] = {}
        for name in ("pg_dump", "pg_restore", "psql"):
            path = binary_root / name
            try:
                information = os.lstat(str(path))
            except OSError as error:
                for opened in descriptors.values():
                    os.close(opened)
                raise RecoveryEnvironmentUnavailable("RECOVERY_TOOLCHAIN_UNAVAILABLE") from error
            if (
                stat.S_ISLNK(information.st_mode)
                or not stat.S_ISREG(information.st_mode)
                or information.st_uid not in (0, os.getuid())
                or stat.S_IMODE(information.st_mode) & 0o022
                or not os.access(str(path), os.X_OK)
            ):
                for opened in descriptors.values():
                    os.close(opened)
                raise RecoveryEnvironmentUnavailable("RECOVERY_TOOLCHAIN_UNAVAILABLE")
            try:
                descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError as error:
                for opened in descriptors.values():
                    os.close(opened)
                raise RecoveryEnvironmentUnavailable("RECOVERY_TOOLCHAIN_UNAVAILABLE") from error
            descriptors[name] = descriptor
            binary_paths[name] = path
            binary_identities[name] = self._stat_identity(os.fstat(descriptor))
        service_descriptor = -1
        try:
            service_descriptor = os.open(
                str(service_file), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            service_info = os.fstat(service_descriptor)
            if (
                not stat.S_ISREG(service_info.st_mode)
                or service_info.st_uid != os.getuid()
                or stat.S_IMODE(service_info.st_mode) != 0o600
                or service_info.st_size > _MAX_SERVICE_FILE_BYTES
                or service_file.resolve(strict=True) != service_file
            ):
                raise RecoverySetError("RECOVERY_SERVICE_FILE_INVALID")
            service_digest, _service_size = self._hash_descriptor(
                service_descriptor, _MAX_SERVICE_FILE_BYTES
            )
        except Exception:
            for descriptor in descriptors.values():
                os.close(descriptor)
            if service_descriptor >= 0:
                os.close(service_descriptor)
            raise
        self._binary_descriptors = descriptors
        self._binary_paths = binary_paths
        self._binary_identities = binary_identities
        self._service_descriptor = service_descriptor
        self._service_path = service_file
        self._service_identity = self._stat_identity(service_info)
        self._service_digest = service_digest
        self._service = service_name
        self._environment = {
            "PGSERVICEFILE": "/dev/fd/" + str(service_descriptor),
            "PGOPTIONS": "-c search_path=pg_catalog,public",
            "LC_ALL": "C",
            "LANG": "C",
        }
        self._timeout = timeout_seconds
        self._closed = False

    @staticmethod
    def _stat_identity(information: os.stat_result) -> Tuple[int, int, int, int, int, int]:
        return (
            information.st_dev,
            information.st_ino,
            information.st_uid,
            stat.S_IMODE(information.st_mode),
            information.st_size,
            information.st_mtime_ns,
        )

    @staticmethod
    def _hash_descriptor(descriptor: int, max_bytes: int) -> Tuple[str, int]:
        original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
                digest.update(chunk)
        finally:
            os.lseek(descriptor, original_offset, os.SEEK_SET)
        return digest.hexdigest(), size

    def _revalidate_bindings(self) -> None:
        if self._closed:
            raise RecoverySetError("RECOVERY_TOOLCHAIN_CHANGED")
        try:
            if self._stat_identity(os.lstat(str(self._service_path))) != self._service_identity:
                raise RecoverySetError("RECOVERY_SERVICE_FILE_CHANGED")
            if self._stat_identity(os.fstat(self._service_descriptor)) != self._service_identity:
                raise RecoverySetError("RECOVERY_SERVICE_FILE_CHANGED")
            digest, _size = self._hash_descriptor(self._service_descriptor, _MAX_SERVICE_FILE_BYTES)
            if digest != self._service_digest:
                raise RecoverySetError("RECOVERY_SERVICE_FILE_CHANGED")
            os.lseek(self._service_descriptor, 0, os.SEEK_SET)
            for name, path in self._binary_paths.items():
                expected = self._binary_identities[name]
                if self._stat_identity(os.lstat(str(path))) != expected:
                    raise RecoverySetError("RECOVERY_TOOLCHAIN_CHANGED")
                if self._stat_identity(os.fstat(self._binary_descriptors[name])) != expected:
                    raise RecoverySetError("RECOVERY_TOOLCHAIN_CHANGED")
        except OSError as error:
            raise RecoverySetError("RECOVERY_TOOLCHAIN_CHANGED") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in (*self._binary_descriptors.values(), self._service_descriptor):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> SubprocessPostgresRecoveryTools:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _service_argument(self) -> str:
        return "service=" + self._service

    def _command(self, binary: str, arguments: Sequence[str]) -> Tuple[list[str], Tuple[int, ...]]:
        return (
            [str(self._binary_paths[binary]), *arguments],
            (self._service_descriptor,),
        )

    def dump(self, destination: Path, max_bytes: int) -> Tuple[str, int]:
        self._revalidate_bindings()
        descriptor = os.open(
            str(destination),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        process: Optional[subprocess.Popen[bytes]] = None
        total = 0
        digest = hashlib.sha256()
        selector: Optional[selectors.BaseSelector] = None
        try:
            command, inherited = self._command(
                "pg_dump",
                (
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname=" + self._service_argument(),
                ),
            )
            process = subprocess.Popen(
                command,
                env=self._environment,
                pass_fds=inherited,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdout is None:
                raise RecoverySetError("RECOVERY_DATABASE_DUMP_FAILED")
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + self._timeout
            with os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        raise RecoverySetError("RECOVERY_DATABASE_DUMP_FAILED")
                    if not selector.select(min(remaining, 1.0)):
                        if process.poll() is None:
                            continue
                    chunk = os.read(
                        process.stdout.fileno(), min(1024 * 1024, max_bytes - total + 1)
                    )
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        process.kill()
                        raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            try:
                return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise RecoverySetError("RECOVERY_DATABASE_DUMP_FAILED") from error
            if return_code != 0 or total < 1:
                raise RecoverySetError("RECOVERY_DATABASE_DUMP_FAILED")
            self._revalidate_bindings()
            return digest.hexdigest(), total
        finally:
            if selector is not None:
                selector.close()
            if descriptor >= 0:
                os.close(descriptor)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    def _run(
        self,
        binary: str,
        arguments: Sequence[str],
        source: Optional[BinaryIO] = None,
        *,
        capture_limit: int = 0,
    ) -> bytes:
        command, inherited = self._command(binary, arguments)
        self._revalidate_bindings()
        if capture_limit:
            process: Optional[subprocess.Popen[bytes]] = None
            selector: Optional[selectors.BaseSelector] = None
            try:
                process = subprocess.Popen(
                    command,
                    env=self._environment,
                    pass_fds=inherited,
                    shell=False,
                    stdin=source if source is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if process.stdout is None:
                    raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED")
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + self._timeout
                output = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED")
                    if not selector.select(min(remaining, 1.0)):
                        if process.poll() is None:
                            continue
                    chunk = os.read(process.stdout.fileno(), capture_limit - len(output) + 1)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > capture_limit:
                        process.kill()
                        raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED")
                try:
                    return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    process.kill()
                    process.wait()
                    raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED") from error
                if return_code != 0:
                    raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED")
                self._revalidate_bindings()
                return bytes(output)
            except OSError as error:
                raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED") from error
            finally:
                if selector is not None:
                    selector.close()
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait()
        try:
            completed = subprocess.run(
                command,
                env=self._environment,
                pass_fds=inherited,
                shell=False,
                stdin=source if source is not None else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED") from error
        if completed.returncode != 0:
            raise RecoverySetError("RECOVERY_DATABASE_TOOL_FAILED")
        self._revalidate_bindings()
        return b""

    def database_identity(self) -> str:
        output = self._run(
            "psql",
            (
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "--dbname=" + self._service_argument(),
                "--command",
                _DATABASE_IDENTITY_SQL,
            ),
            capture_limit=512,
        ).strip()
        if not output or b"\n" in output:
            raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_INVALID")
        return hashlib.sha256(output).hexdigest()

    def target_database_is_empty(self) -> bool:
        query = (
            "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
            "WHERE c.relkind IN ('r','p','v','m','S','f') "
            "AND n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname !~ '^pg_toast'"
        )
        output = self._run(
            "psql",
            (
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "--dbname=" + self._service_argument(),
                "--command",
                query,
            ),
            capture_limit=128,
        )
        try:
            count = int(output.strip())
        except ValueError as error:
            raise RecoverySetError("RECOVERY_DATABASE_PREFLIGHT_FAILED") from error
        return count == 0

    def target_database_has_sealed_migration_ledger(self) -> bool:
        output = self._run(
            "psql",
            (
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "--dbname=" + self._service_argument(),
                "--command",
                _LEDGER_AUTHORITY_SQL,
            ),
            capture_limit=128,
        )
        return output.strip() == b"1"

    def restore(self, source: Path, expected_sha256: str, max_bytes: int) -> None:
        self._revalidate_bindings()
        descriptor = os.open(str(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            information = os.fstat(descriptor)
            identity = self._stat_identity(information)
            if (
                not stat.S_ISREG(information.st_mode)
                or information.st_uid != os.getuid()
                or stat.S_IMODE(information.st_mode) != 0o600
            ):
                raise RecoverySetError("RECOVERY_FILE_INVALID")
            observed_sha256, size = self._hash_descriptor(descriptor, max_bytes)
            if size < 1 or observed_sha256 != expected_sha256:
                raise RecoverySetError("RECOVERY_DATABASE_INTEGRITY_FAILED")
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                self._run(
                    "pg_restore",
                    (
                        "--single-transaction",
                        "--exit-on-error",
                        "--no-owner",
                        "--no-privileges",
                        "--dbname=" + self._service_argument(),
                    ),
                    stream,
                )
            if self._stat_identity(os.fstat(descriptor)) != identity:
                raise RecoverySetError("RECOVERY_DATABASE_INTEGRITY_FAILED")
            observed_after, size_after = self._hash_descriptor(descriptor, max_bytes)
            if (observed_after, size_after) != (expected_sha256, size):
                raise RecoverySetError("RECOVERY_DATABASE_INTEGRITY_FAILED")
            self._revalidate_bindings()
        finally:
            os.close(descriptor)


def create_recovery_set(
    tools: PostgresRecoveryTools,
    canonical_root: Path,
    recovery_set: Path,
    limits: RecoveryLimits = _DEFAULT_LIMITS,
    progress_hook: Callable[[str], None] = _no_progress,
) -> RecoveryEvidence:
    """Create a no-overwrite recovery set with restartable staged publication."""

    source_root = _private_absolute_directory(canonical_root, existing=True)
    destination = _private_absolute_target(recovery_set)
    _require_disjoint(source_root, destination)
    source_database_identity = tools.database_identity()
    if not _SHA256.fullmatch(source_database_identity):
        raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_INVALID")
    if not tools.target_database_has_sealed_migration_ledger():
        raise RecoverySetError("RECOVERY_DATABASE_AUTHORITY_INVALID")
    stage, state_path = _operation_paths(destination, "backup")
    try:
        state = _read_operation_state(state_path, "backup")
    except RecoverySetError as error:
        if error.code != "RECOVERY_OPERATION_STATE_INVALID" or state_path.exists():
            raise
        state = None
    if state is not None:
        if state.get("database_identity") != source_database_identity:
            raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_CHANGED")
        phase = state["phase"]
        if phase == "building":
            if stage.exists() and (stage / "manifest.json").exists():
                completed = _validate_recovery_set(stage, limits)
                if completed.source_database_identity != source_database_identity:
                    raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_CHANGED")
                _fsync_tree(stage)
                _write_operation_state(
                    state_path,
                    "backup",
                    "publishing",
                    source_database_identity,
                    completed.manifest_sha256,
                )
                state = _read_operation_state(state_path, "backup")
                phase = "publishing"
            else:
                _remove_owned_stage(stage)
                _unlink_durable(state_path)
                state = None
        elif phase == "publishing":
            pass
        else:
            raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
        if state is not None and phase == "publishing":
            expected_manifest = state.get("manifest_sha256")
            if destination.exists() and not stage.exists():
                completed = _validate_recovery_set(destination, limits)
                if (
                    completed.manifest_sha256 != expected_manifest
                    or completed.source_database_identity != source_database_identity
                ):
                    raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
                _unlink_durable(state_path)
                return RecoveryEvidence(
                    len(completed.objects),
                    sum(item.size_bytes for item in completed.objects),
                    completed.dump_size_bytes,
                )
            if stage.exists() and not destination.exists():
                completed = _validate_recovery_set(stage, limits)
                if (
                    completed.manifest_sha256 != expected_manifest
                    or completed.source_database_identity != source_database_identity
                ):
                    raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
                _require_database_identity(tools, source_database_identity)
                _publish_directory(stage, destination)
                progress_hook("after_publish")
                _unlink_durable(state_path)
                return RecoveryEvidence(
                    len(completed.objects),
                    sum(item.size_bytes for item in completed.objects),
                    completed.dump_size_bytes,
                )
            raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
    if destination.exists():
        raise RecoverySetError("RECOVERY_TARGET_EXISTS")
    if stage.exists():
        raise RecoverySetError("RECOVERY_OPERATION_STAGE_INVALID")
    _write_operation_state(state_path, "backup", "building", source_database_identity)
    os.mkdir(str(stage), 0o700)
    dump_path = stage / "database.dump"
    canonical_destination = stage / "canonical"
    os.mkdir(str(canonical_destination), 0o700)
    dump_digest, dump_size = tools.dump(dump_path, limits.max_database_dump_bytes)
    progress_hook("after_dump")
    if dump_size < 1 or not _SHA256.fullmatch(dump_digest):
        raise RecoverySetError("RECOVERY_DATABASE_DUMP_FAILED")
    if dump_size > limits.max_temporary_bytes:
        raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
    source_store = LocalBlobStore(source_root)
    objects = []
    total = 0
    try:
        for key in _canonical_keys(source_root, limits):
            with source_store.open_reader(key) as reader:
                digest, size = _copy_stream(
                    reader,
                    canonical_destination / key,
                    min(
                        limits.max_single_blob_bytes,
                        limits.max_total_blob_bytes - total,
                        limits.max_temporary_bytes - dump_size - total,
                    ),
                )
            expected = key.rsplit("/", 1)[-1]
            if digest != expected:
                raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED")
            total += size
            if total > limits.max_total_blob_bytes:
                raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
            objects.append(RecoveryObject(key, digest, size))
    finally:
        source_store.close()
    progress_hook("after_canonical_copy")
    _atomic_json(
        stage / "manifest.json",
        {
            "schema": _SCHEMA,
            "database": {
                "format": "postgres-custom",
                "sha256": dump_digest,
                "size_bytes": dump_size,
                "source_database_identity": source_database_identity,
            },
            "canonical": {
                "objects": [
                    {
                        "storage_key": item.storage_key,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in objects
                ],
                "total_bytes": total,
            },
        },
    )
    _fsync_tree(stage)
    progress_hook("after_manifest_complete")
    manifest_digest, _manifest_size = _hash_file(stage / "manifest.json", _MAX_MANIFEST_BYTES)
    _require_database_identity(tools, source_database_identity)
    _write_operation_state(
        state_path,
        "backup",
        "publishing",
        source_database_identity,
        manifest_digest,
    )
    progress_hook("after_manifest")
    _publish_directory(stage, destination)
    progress_hook("after_publish")
    _unlink_durable(state_path)
    return RecoveryEvidence(len(objects), total, dump_size)


def restore_recovery_set(
    tools: PostgresRecoveryTools,
    recovery_set: Path,
    canonical_target: Path,
    limits: RecoveryLimits = _DEFAULT_LIMITS,
    progress_hook: Callable[[str], None] = _no_progress,
) -> RecoveryEvidence:
    """Restore through a restartable canonical stage and a single DB transaction.

    A failure from ``pg_restore`` is retryable because the subprocess contract
    uses ``--single-transaction``.  The target database must remain offline and
    exclusively owned by the drill: while the durable state is
    ``database_restoring``, an empty database means retry and a non-empty
    database means the single transaction committed before a process crash.
    """

    source = _private_absolute_directory(recovery_set, existing=True)
    target = _private_absolute_target(canonical_target)
    _require_disjoint(source, target)
    validated = _validate_recovery_set(source, limits)
    manifest_digest = validated.manifest_sha256
    dump_size = validated.dump_size_bytes
    objects = validated.objects
    source_canonical = source / "canonical"
    target_database_identity = tools.database_identity()
    if not _SHA256.fullmatch(target_database_identity):
        raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_INVALID")
    stage, state_path = _operation_paths(target, "restore")
    try:
        state = _read_operation_state(state_path, "restore")
    except RecoverySetError as error:
        if error.code != "RECOVERY_OPERATION_STATE_INVALID" or state_path.exists():
            raise
        state = None
    if state is None:
        if target.exists():
            raise RecoverySetError("RECOVERY_TARGET_EXISTS")
        if stage.exists():
            raise RecoverySetError("RECOVERY_OPERATION_STAGE_INVALID")
        if not tools.target_database_is_empty():
            raise RecoverySetError("RECOVERY_DATABASE_NOT_EMPTY")
        _write_operation_state(
            state_path,
            "restore",
            "preparing",
            target_database_identity,
            manifest_digest,
        )
        phase = "preparing"
    else:
        if state.get("manifest_sha256") != manifest_digest:
            raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
        if state.get("database_identity") != target_database_identity:
            raise RecoverySetError("RECOVERY_DATABASE_IDENTITY_CHANGED")
        phase = str(state["phase"])
    if phase == "preparing":
        if not tools.target_database_is_empty():
            raise RecoverySetError("RECOVERY_DATABASE_AMBIGUOUS")
        _remove_owned_stage(stage)
        os.mkdir(str(stage), 0o700)
        source_store = LocalBlobStore(source_canonical)
        restored_total = 0
        try:
            for item in objects:
                with source_store.open_reader(item.storage_key) as reader:
                    digest, size = _copy_stream(
                        reader,
                        stage / item.storage_key,
                        min(
                            item.size_bytes,
                            limits.max_single_blob_bytes,
                            limits.max_total_blob_bytes - restored_total,
                            limits.max_temporary_bytes - restored_total,
                        ),
                    )
                if (digest, size, item.storage_key) != (
                    item.sha256,
                    item.size_bytes,
                    item.storage_key,
                ):
                    raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED")
                restored_total += size
        finally:
            source_store.close()
        staged_keys = _canonical_keys(stage, limits)
        if staged_keys != tuple(item.storage_key for item in objects):
            raise RecoverySetError("RECOVERY_CANONICAL_INTEGRITY_FAILED")
        _fsync_tree(stage)
        _write_operation_state(
            state_path,
            "restore",
            "prepared",
            target_database_identity,
            manifest_digest,
        )
        progress_hook("after_canonical_stage")
        phase = "prepared"
    if phase == "prepared":
        if not tools.target_database_is_empty():
            raise RecoverySetError("RECOVERY_DATABASE_AMBIGUOUS")
        _validate_canonical_root(stage, objects, limits)
        _require_database_identity(tools, target_database_identity)
        _write_operation_state(
            state_path,
            "restore",
            "database_restoring",
            target_database_identity,
            manifest_digest,
        )
        progress_hook("before_database_restore")
        phase = "database_restoring"
    if phase == "database_restoring":
        if tools.target_database_is_empty():
            tools.restore(
                source / "database.dump",
                validated.dump_sha256,
                limits.max_database_dump_bytes,
            )
            progress_hook("after_database_commit_before_marker")
        _require_database_identity(tools, target_database_identity)
        if not tools.target_database_has_sealed_migration_ledger():
            raise RecoverySetError("RECOVERY_DATABASE_AUTHORITY_INVALID")
        _write_operation_state(
            state_path,
            "restore",
            "database_restored",
            target_database_identity,
            manifest_digest,
        )
        progress_hook("after_database_restore")
        phase = "database_restored"
    if phase != "database_restored":
        raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
    if not tools.target_database_has_sealed_migration_ledger():
        raise RecoverySetError("RECOVERY_DATABASE_AUTHORITY_INVALID")
    _require_database_identity(tools, target_database_identity)
    if target.exists() and not stage.exists():
        _validate_canonical_root(target, objects, limits)
    elif stage.exists() and not target.exists():
        _validate_canonical_root(stage, objects, limits)
        _publish_directory(stage, target)
        progress_hook("after_canonical_publish")
    else:
        raise RecoverySetError("RECOVERY_OPERATION_STATE_INVALID")
    _unlink_durable(state_path)
    return RecoveryEvidence(len(objects), sum(item.size_bytes for item in objects), dump_size)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or restore a bounded PostgreSQL plus canonical-blob recovery set."
    )
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--postgres-bin-root", required=True)
    parser.add_argument("--pg-service-file", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--recovery-set", required=True)
    parser.add_argument("--canonical-root", required=True)
    parser.add_argument("--max-objects", type=int, default=100_000)
    parser.add_argument("--max-single-blob-bytes", type=int, default=1 << 36)
    parser.add_argument("--max-blob-bytes", type=int, default=1 << 40)
    parser.add_argument("--max-dump-bytes", type=int, default=1 << 40)
    parser.add_argument("--max-temporary-bytes", type=int, default=2 << 40)
    options = parser.parse_args(argv)
    tools: Optional[SubprocessPostgresRecoveryTools] = None
    try:
        limits = RecoveryLimits(
            max_objects=options.max_objects,
            max_single_blob_bytes=options.max_single_blob_bytes,
            max_total_blob_bytes=options.max_blob_bytes,
            max_database_dump_bytes=options.max_dump_bytes,
            max_temporary_bytes=options.max_temporary_bytes,
        )
        tools = SubprocessPostgresRecoveryTools(
            Path(options.postgres_bin_root), Path(options.pg_service_file), options.service
        )
        if options.action == "backup":
            evidence = create_recovery_set(
                tools, Path(options.canonical_root), Path(options.recovery_set), limits
            )
            outcome = "RECOVERY_SET_CREATED"
        else:
            evidence = restore_recovery_set(
                tools, Path(options.recovery_set), Path(options.canonical_root), limits
            )
            outcome = "RECOVERY_SET_RESTORED"
    except RecoveryEnvironmentUnavailable as error:
        sys.stderr.write(error.code + "\n")
        return 3
    except (OSError, ValueError, RecoverySetError):
        sys.stderr.write("RECOVERY_OPERATION_FAILED\n")
        return 2
    finally:
        if tools is not None:
            tools.close()
    sys.stdout.write(
        json.dumps(
            {
                "outcome": outcome,
                "object_count": evidence.object_count,
                "blob_bytes": evidence.blob_bytes,
                "database_dump_bytes": evidence.database_dump_bytes,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
