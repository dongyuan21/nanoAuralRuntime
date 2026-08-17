"""Ordered, checksum-sealed PostgreSQL SQL migrations.

The migration ledger is bootstrap metadata rather than an application schema
migration: it exists before the first ``*.sql`` file can be recorded.  Every
new row records the SHA-256 of the exact SQL bytes executed in the same
transaction as that SQL.  A historical filename-only ledger is never trusted
or upgraded implicitly; an operator must explicitly adopt release-trusted
digests before normal migration can continue.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, ContextManager, Mapping, Optional, Protocol, Sequence, Tuple

_ADVISORY_LOCK_ID = 734931706
_MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHECK_EXPRESSION = "(sha256 ~ '^[0-9a-f]{64}$'::text)"
_RUNTIME_ROLE = "nano_aural_runtime"
_LEDGER_FUNCTION_SOURCE = """DECLARE
    authorized BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        BEGIN
            EXECUTE 'SELECT EXISTS (SELECT 1 FROM pg_temp.nano_aural_migration_authorization WHERE token = current_setting(''nano_aural.migration_runner_token'', true))'
                INTO authorized;
        EXCEPTION
            WHEN undefined_table OR invalid_schema_name THEN
                authorized := FALSE;
        END;
        IF authorized THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'schema migration ledger rows require the migration runner';
END"""


class MigrationIntegrityError(RuntimeError):
    """The migration source and durable ledger cannot be safely reconciled."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PostgresMigrationConnection(Protocol):
    """Small structural surface required from a psycopg3 connection."""

    def transaction(self) -> ContextManager[object]: ...

    def execute(
        self,
        query: str,
        params: Optional[Sequence[object]] = None,
        *,
        prepare: Optional[bool] = None,
    ) -> Any: ...


@dataclass(frozen=True)
class MigrationSource:
    filename: str
    sql: str
    sha256: str


def default_migrations_dir() -> Path:
    """Development-tree mirror location (installed wheels use package data)."""

    return Path(__file__).resolve().parents[3] / "migrations"


def _decode_source(filename: str, data: bytes) -> MigrationSource:
    if not _MIGRATION_NAME.fullmatch(filename):
        raise MigrationIntegrityError("MIGRATION_SOURCE_NAME_INVALID")
    try:
        sql = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MigrationIntegrityError("MIGRATION_SOURCE_ENCODING_INVALID") from error
    if not sql.strip():
        raise MigrationIntegrityError("MIGRATION_SOURCE_EMPTY")
    return MigrationSource(filename, sql, hashlib.sha256(data).hexdigest())


def _migration_sources(migrations_dir: Optional[Path]) -> Tuple[MigrationSource, ...]:
    if migrations_dir is not None:
        directory = Path(migrations_dir)
        entries = tuple(
            _decode_source(path.name, path.read_bytes()) for path in sorted(directory.glob("*.sql"))
        )
    else:
        directory = resources.files("nano_aural_runtime.durable").joinpath("sql")
        entries = tuple(
            _decode_source(entry.name, entry.read_bytes())
            for entry in sorted(directory.iterdir(), key=lambda entry: entry.name)
            if entry.name.endswith(".sql")
        )
    if not entries:
        raise MigrationIntegrityError("MIGRATION_SOURCE_SET_EMPTY")
    filenames = tuple(item.filename for item in entries)
    if len(set(filenames)) != len(filenames):
        raise MigrationIntegrityError("MIGRATION_SOURCE_NAME_DUPLICATE")
    numbers = tuple(int(filename[:4]) for filename in filenames)
    if numbers != tuple(range(1, len(entries) + 1)):
        raise MigrationIntegrityError("MIGRATION_SOURCE_SEQUENCE_INVALID")
    return entries


def _prepare_ledger(connection: PostgresMigrationConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS nano_aural_schema_migrations (
            filename TEXT PRIMARY KEY,
            sha256 TEXT,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    connection.execute(
        "ALTER TABLE nano_aural_schema_migrations ADD COLUMN IF NOT EXISTS sha256 TEXT"
    )


def _ledger_rows(connection: PostgresMigrationConnection) -> Tuple[Tuple[str, Optional[str]], ...]:
    rows = connection.execute(
        "SELECT filename,sha256 FROM nano_aural_schema_migrations ORDER BY filename"
    ).fetchall()
    return tuple((str(row[0]), str(row[1]) if row[1] is not None else None) for row in rows)


def _validate_prefix(
    sources: Sequence[MigrationSource], rows: Sequence[Tuple[str, Optional[str]]]
) -> None:
    expected = tuple(item.filename for item in sources[: len(rows)])
    observed = tuple(filename for filename, _digest in rows)
    if len(rows) > len(sources) or observed != expected:
        raise MigrationIntegrityError("MIGRATION_LEDGER_NOT_ORDERED_PREFIX")


def _validate_sealed_rows(
    sources: Sequence[MigrationSource], rows: Sequence[Tuple[str, Optional[str]]]
) -> None:
    _validate_prefix(sources, rows)
    for source, (_filename, digest) in zip(sources, rows):
        if digest is None:
            raise MigrationIntegrityError("MIGRATION_LEGACY_CHECKSUMS_REQUIRED")
        if not _SHA256.fullmatch(digest) or digest != source.sha256:
            raise MigrationIntegrityError("MIGRATION_CHECKSUM_MISMATCH")


def _seal_objects_present(connection: PostgresMigrationConnection) -> bool:
    row = connection.execute(
        """SELECT
             EXISTS (
               SELECT 1 FROM pg_catalog.pg_constraint
               WHERE conrelid='public.nano_aural_schema_migrations'::pg_catalog.regclass
                 AND conname='nano_aural_schema_migrations_sha256_format'
             ),
             EXISTS (
               SELECT 1 FROM pg_catalog.pg_proc p
               JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
               WHERE n.nspname='public'
                 AND p.proname='nano_aural_guard_schema_migration_ledger'
             ),
             EXISTS (
               SELECT 1 FROM pg_catalog.pg_trigger
               WHERE tgrelid='public.nano_aural_schema_migrations'::pg_catalog.regclass
                 AND tgname='nano_aural_schema_migrations_guard'
                 AND NOT tgisinternal
             )"""
    ).fetchone()
    return row is not None and any(bool(item) for item in row)


def _install_ledger_seal(connection: PostgresMigrationConnection) -> None:
    if _seal_objects_present(connection):
        raise MigrationIntegrityError("MIGRATION_LEDGER_SEAL_CONFLICT")
    connection.execute(
        """ALTER TABLE public.nano_aural_schema_migrations
           ADD CONSTRAINT nano_aural_schema_migrations_sha256_format
           CHECK (sha256 ~ '^[0-9a-f]{64}$')"""
    )
    connection.execute(
        """ALTER TABLE public.nano_aural_schema_migrations
           ALTER COLUMN sha256 SET NOT NULL"""
    )
    connection.execute(
        """REVOKE INSERT,UPDATE,DELETE
           ON TABLE public.nano_aural_schema_migrations FROM PUBLIC"""
    )
    connection.execute(
        """CREATE FUNCTION public.nano_aural_guard_schema_migration_ledger()
           RETURNS trigger LANGUAGE plpgsql AS $function$"""
        + _LEDGER_FUNCTION_SOURCE
        + "$function$",
        prepare=False,
    )
    connection.execute(
        """CREATE TRIGGER nano_aural_schema_migrations_guard
           BEFORE INSERT OR UPDATE OR DELETE ON public.nano_aural_schema_migrations
           FOR EACH ROW EXECUTE FUNCTION public.nano_aural_guard_schema_migration_ledger()"""
    )


def _verify_ledger_seal(connection: PostgresMigrationConnection) -> None:
    row = connection.execute(
        """SELECT
             a.attnotnull,a.atttypid='pg_catalog.text'::pg_catalog.regtype,
             c.contype,c.convalidated,pg_catalog.pg_get_expr(c.conbin,c.conrelid),
             t.tgenabled,t.tgtype,p.prosrc,p.prosecdef,p.proleakproof,p.pronargs,
             np.nspname,pg_catalog.pg_get_userbyid(p.proowner)=current_user,
             NOT EXISTS (
               SELECT 1
               FROM pg_catalog.aclexplode(
                 COALESCE(r.relacl,pg_catalog.acldefault('r',r.relowner))
               ) AS permission
               WHERE permission.privilege_type IN ('INSERT','UPDATE','DELETE')
                 AND permission.grantee<>r.relowner
             ) AND NOT EXISTS (
               SELECT 1
               FROM pg_catalog.pg_attribute AS protected_column
               CROSS JOIN LATERAL pg_catalog.aclexplode(
                 protected_column.attacl
               ) AS permission
               WHERE protected_column.attrelid=r.oid
                 AND protected_column.attnum>0 AND NOT protected_column.attisdropped
                 AND protected_column.attacl IS NOT NULL
                 AND permission.privilege_type IN ('INSERT','UPDATE','DELETE')
                 AND permission.grantee<>r.relowner
             )
           FROM pg_catalog.pg_class r
           JOIN pg_catalog.pg_namespace nr ON nr.oid=r.relnamespace
           JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid AND a.attname='sha256'
           JOIN pg_catalog.pg_constraint c ON c.conrelid=r.oid
             AND c.conname='nano_aural_schema_migrations_sha256_format'
           JOIN pg_catalog.pg_trigger t ON t.tgrelid=r.oid
             AND t.tgname='nano_aural_schema_migrations_guard' AND NOT t.tgisinternal
           JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
           JOIN pg_catalog.pg_namespace np ON np.oid=p.pronamespace
           WHERE nr.nspname='public' AND r.relname='nano_aural_schema_migrations'
             AND p.proname='nano_aural_guard_schema_migration_ledger'"""
    ).fetchone()
    expected = (
        True,
        True,
        "c",
        True,
        _CHECK_EXPRESSION,
        "O",
        31,
        _LEDGER_FUNCTION_SOURCE,
        False,
        False,
        0,
        "public",
        True,
        True,
    )
    if row is None or tuple(row) != expected:
        raise MigrationIntegrityError("MIGRATION_LEDGER_SEAL_INVALID")
    duplicates = connection.execute(
        """SELECT
             (SELECT count(*) FROM pg_catalog.pg_constraint
              WHERE conrelid='public.nano_aural_schema_migrations'::pg_catalog.regclass
                AND conname='nano_aural_schema_migrations_sha256_format'),
             (SELECT count(*) FROM pg_catalog.pg_trigger
              WHERE tgrelid='public.nano_aural_schema_migrations'::pg_catalog.regclass
                AND tgname='nano_aural_schema_migrations_guard' AND NOT tgisinternal),
             (SELECT count(*) FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
              WHERE n.nspname='public'
                AND p.proname='nano_aural_guard_schema_migration_ledger')"""
    ).fetchone()
    if duplicates is None or tuple(int(item) for item in duplicates) != (1, 1, 1):
        raise MigrationIntegrityError("MIGRATION_LEDGER_SEAL_INVALID")


def _authorize_ledger_inserts(connection: PostgresMigrationConnection) -> None:
    token = secrets.token_hex(32)
    connection.execute(
        """CREATE TEMPORARY TABLE nano_aural_migration_authorization (
             token TEXT PRIMARY KEY
           ) ON COMMIT DROP"""
    )
    connection.execute(
        "INSERT INTO pg_temp.nano_aural_migration_authorization(token) VALUES (%s)", (token,)
    )
    connection.execute(
        "SELECT pg_catalog.set_config('nano_aural.migration_runner_token',%s,true)", (token,)
    )


def apply_postgres_migrations(
    connection: PostgresMigrationConnection, migrations_dir: Optional[Path] = None
) -> None:
    """Verify the ordered ledger, then atomically apply every pending SQL file.

    ``MIGRATION_LEGACY_CHECKSUMS_REQUIRED`` means an old filename-only ledger
    must first be processed with :func:`adopt_legacy_postgres_migration_checksums`.
    Normal migration deliberately cannot infer historical SQL bytes.
    """

    sources = _migration_sources(migrations_dir)
    with connection.transaction():
        connection.execute("SET LOCAL search_path TO public, pg_catalog")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_ID,))
        _prepare_ledger(connection)
        rows = _ledger_rows(connection)
        _validate_sealed_rows(sources, rows)
        if rows:
            _verify_ledger_seal(connection)
        elif _seal_objects_present(connection):
            raise MigrationIntegrityError("MIGRATION_LEDGER_SEAL_CONFLICT")
        if len(rows) < len(sources) and rows:
            _authorize_ledger_inserts(connection)
        for source in sources[len(rows) :]:
            connection.execute(source.sql, prepare=False)
            connection.execute(
                """INSERT INTO nano_aural_schema_migrations (filename,sha256)
                   VALUES (%s,%s)""",
                (source.filename, source.sha256),
            )
        if not rows:
            _install_ledger_seal(connection)
        _verify_ledger_seal(connection)


def verify_postgres_migrations(
    connection: PostgresMigrationConnection, migrations_dir: Optional[Path] = None
) -> None:
    """Read and verify a fully applied ledger without changing database state."""

    sources = _migration_sources(migrations_dir)
    with connection.transaction():
        connection.execute("SET LOCAL search_path TO public, pg_catalog")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_ID,))
        table = connection.execute(
            "SELECT to_regclass('public.nano_aural_schema_migrations')"
        ).fetchone()
        if table is None or table[0] is None:
            raise MigrationIntegrityError("MIGRATION_LEDGER_MISSING")
        column = connection.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema='public'
                 AND table_name='nano_aural_schema_migrations'
                 AND column_name='sha256'"""
        ).fetchone()
        if column is None:
            raise MigrationIntegrityError("MIGRATION_LEGACY_CHECKSUMS_REQUIRED")
        rows = _ledger_rows(connection)
        _validate_sealed_rows(sources, rows)
        if len(rows) != len(sources):
            raise MigrationIntegrityError("MIGRATION_PENDING")
        _verify_ledger_seal(connection)


def grant_postgres_runtime_role(connection: PostgresMigrationConnection, runtime_role: str) -> None:
    """Idempotently grant least-privilege application access after migration.

    The role is deliberately fixed so no caller-controlled identifier reaches
    SQL.  PostgreSQL initialization owns credential creation; this operation
    only validates that non-privileged role and grants application access.
    """

    if runtime_role != _RUNTIME_ROLE:
        raise MigrationIntegrityError("MIGRATION_RUNTIME_ROLE_INVALID")
    sources = _migration_sources(None)
    with connection.transaction():
        connection.execute("SET LOCAL search_path TO public, pg_catalog")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_ID,))
        table = connection.execute(
            "SELECT to_regclass('public.nano_aural_schema_migrations')"
        ).fetchone()
        if table is None or table[0] is None:
            raise MigrationIntegrityError("MIGRATION_LEDGER_MISSING")
        rows = _ledger_rows(connection)
        _validate_sealed_rows(sources, rows)
        if len(rows) != len(sources):
            raise MigrationIntegrityError("MIGRATION_PENDING")
        _verify_ledger_seal(connection)
        role = connection.execute(
            """SELECT
                 r.rolcanlogin,NOT r.rolsuper,NOT r.rolinherit,
                 NOT r.rolcreaterole,NOT r.rolcreatedb,NOT r.rolreplication,
                 NOT r.rolbypassrls,
                 c.relowner<>r.oid,
                 pg_catalog.pg_get_userbyid(c.relowner)=current_user,
                 NOT EXISTS (
                   SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                   WHERE membership.member=r.oid
                 )
               FROM pg_catalog.pg_roles AS r
               JOIN pg_catalog.pg_class AS c
                 ON c.oid='public.nano_aural_schema_migrations'::pg_catalog.regclass
               WHERE r.rolname='nano_aural_runtime'"""
        ).fetchone()
        if role is None or tuple(role) != (True,) * 10:
            raise MigrationIntegrityError("MIGRATION_RUNTIME_ROLE_UNSAFE")
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("REVOKE CREATE ON SCHEMA public FROM nano_aural_runtime")
        connection.execute("GRANT USAGE ON SCHEMA public TO nano_aural_runtime")
        connection.execute(
            """GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public
               TO nano_aural_runtime"""
        )
        connection.execute(
            """GRANT SELECT,USAGE,UPDATE ON ALL SEQUENCES IN SCHEMA public
               TO nano_aural_runtime"""
        )
        connection.execute(
            """REVOKE ALL PRIVILEGES ON TABLE public.nano_aural_schema_migrations
               FROM nano_aural_runtime"""
        )
        connection.execute(
            """REVOKE INSERT(filename,sha256,applied_at),
                      UPDATE(filename,sha256,applied_at)
               ON TABLE public.nano_aural_schema_migrations
               FROM nano_aural_runtime"""
        )
        connection.execute(
            """GRANT SELECT ON TABLE public.nano_aural_schema_migrations
               TO nano_aural_runtime"""
        )
        connection.execute(
            """ALTER DEFAULT PRIVILEGES IN SCHEMA public
               GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES
               TO nano_aural_runtime"""
        )
        connection.execute(
            """ALTER DEFAULT PRIVILEGES IN SCHEMA public
               GRANT SELECT,USAGE,UPDATE ON SEQUENCES
               TO nano_aural_runtime"""
        )
        _verify_ledger_seal(connection)


def adopt_legacy_postgres_migration_checksums(
    connection: PostgresMigrationConnection,
    trusted_sha256_by_filename: Mapping[str, str],
    migrations_dir: Optional[Path] = None,
) -> None:
    """Explicitly seal a historical filename-only ledger with trusted digests.

    The caller must supply the release-trusted digest for *every* historical
    ledger row.  Adoption succeeds only when those rows are an ordered prefix
    of the available source set and every trusted digest equals the current
    source bytes.  It never applies pending application migrations.
    """

    if not isinstance(trusted_sha256_by_filename, Mapping):
        raise TypeError("trusted_sha256_by_filename must be a mapping")
    trusted = dict(trusted_sha256_by_filename)
    if not trusted or any(
        not isinstance(name, str)
        or not isinstance(digest, str)
        or not _MIGRATION_NAME.fullmatch(name)
        or not _SHA256.fullmatch(digest)
        for name, digest in trusted.items()
    ):
        raise MigrationIntegrityError("MIGRATION_ADOPTION_MANIFEST_INVALID")
    sources = _migration_sources(migrations_dir)
    with connection.transaction():
        connection.execute("SET LOCAL search_path TO public, pg_catalog")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_ID,))
        _prepare_ledger(connection)
        rows = _ledger_rows(connection)
        _validate_prefix(sources, rows)
        if not rows:
            raise MigrationIntegrityError("MIGRATION_ADOPTION_NOT_REQUIRED")
        if set(trusted) != {filename for filename, _digest in rows}:
            raise MigrationIntegrityError("MIGRATION_ADOPTION_MANIFEST_INCOMPLETE")
        if _seal_objects_present(connection):
            raise MigrationIntegrityError("MIGRATION_LEDGER_SEAL_CONFLICT")
        for source, (_filename, ledger_digest) in zip(sources, rows):
            if trusted[source.filename] != source.sha256:
                raise MigrationIntegrityError("MIGRATION_ADOPTION_TRUST_MISMATCH")
            if ledger_digest is not None and ledger_digest != source.sha256:
                raise MigrationIntegrityError("MIGRATION_CHECKSUM_MISMATCH")
            if ledger_digest is None:
                changed = connection.execute(
                    """UPDATE nano_aural_schema_migrations SET sha256=%s
                       WHERE filename=%s AND sha256 IS NULL""",
                    (source.sha256, source.filename),
                ).rowcount
                if changed != 1:
                    raise MigrationIntegrityError("MIGRATION_ADOPTION_CONCURRENT_CHANGE")
        _install_ledger_seal(connection)
        _verify_ledger_seal(connection)
