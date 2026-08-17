# Phase 6 migration and recovery hardening

## Scope, non-goals, tests, and Gate

This Phase 6 non-hardware slice seals migration history and supplies a bounded
PostgreSQL plus canonical-blob recovery-set tool. It covers fresh migration,
repeat migration, upgrades from every historical `0001` through `0005`
prefix, explicit adoption of a legacy filename-only ledger, immutable ledger
enforcement, recovery-set integrity, and restartable backup/restore stages.

It does not run ControlFoley, CUDA, ComfyUI, Docker, or the deferred RTX 4090
suite. It does not make the complete Phase 6 Gate pass. A skipped hardware or
missing-tool check is never success evidence.

The software Gate for this slice requires:

- byte-identical root and packaged migrations;
- real PostgreSQL 16 migration/adoption/tamper tests;
- the backup/restore contract and all fault-injection tests;
- a real `pg_dump`/`pg_restore` drill when those client binaries are present;
- restored migration-ledger, event, winner-catalog, canonical input, and
  downloaded output SHA-256/size verification.

The non-hardware Gate was executed with matching PostgreSQL 16.3 server and
client tools. The focused suite completed 42 tests with no skip, including the
real dump/restore winner, catalog, input, output, and migration-ledger checks.
Future hosts that lack `pg_dump`, `pg_restore`, or `psql` must still report the
drill as **UNRUN_ENV**, never as a pass.

## Checksum-sealed migration ledger

Every newly applied SQL file is decoded as strict UTF-8 and hashed over its
exact bytes. The runner executes the SQL and inserts its filename plus
lowercase SHA-256 in one transaction under the existing advisory transaction
lock. Ledger rows must be an exact ordered prefix of the available migration
set. Missing, unknown, out-of-order, malformed, or checksum-mismatched history
fails closed.

After sealing, the digest is `NOT NULL`, constrained to lowercase SHA-256, and
ledger rows are protected by an exactly verified database trigger/function
pair. It rejects every `UPDATE` or `DELETE` and rejects `INSERT` unless the
migration runner has installed its transaction-local guard. Every invocation
rechecks the exact validated CHECK expression, trigger event mask, enabled
mode, function body/flags/owner, and object cardinality; a same-name no-op,
disabled trigger, or replica-only trigger fails closed.

The transaction-local guard prevents accidental direct inserts by the
migration owner; it is not a secret or a defense against that owner. The
deployment boundary is PostgreSQL ACLs: only the dedicated migration
owner/migrator role may own or insert into the ledger, while application and
worker runtime roles receive `SELECT` only and no ledger `INSERT`, `UPDATE`,
or `DELETE`. The installer revokes those operations from `PUBLIC`. A database
owner/superuser remains the trusted root and can deliberately change ACLs,
disable triggers, or forge guard state; no in-database mechanism can claim to
protect against a malicious owner. Keep migration credentials out of runtime
services. Verification also rejects any table-level or column-level ledger DML
grant to a non-owner role; the migration command must connect as the ledger
owner (or explicitly `SET ROLE` to it). Runtime roles remain unable to append
even if they reproduce the documented transaction-local guard exactly.

A historical ledger containing filenames but no digests is not evidence of
which SQL bytes were executed. Normal migration therefore exits with:

```text
MIGRATION_LEGACY_CHECKSUMS_REQUIRED
```

It never copies the current source digest into those rows automatically.

### Explicit legacy adoption

Obtain the checksum manifest from a separately authenticated release source,
not from the mutable deployment checkout being inspected. The file must be an
absolute, owner-owned regular file, mode `0600` (or stricter), no symlink, and
at most 64 KiB. Its exact schema is:

```json
{
  "schema": "nano-aural-migration-checksums/v1",
  "migrations": [
    {
      "filename": "0001_durable_foundation.sql",
      "sha256": "<release-trusted-lowercase-sha256>"
    }
  ]
}
```

The manifest must contain exactly every existing ledger filename. Adoption
checks that the ledger is an ordered prefix, every trusted digest matches the
current migration bytes, and any already-present digest agrees. Only then does
one transaction fill the legacy digests and install the immutable constraints.
It does not apply pending migrations.

```sh
export NANO_AURAL_DATABASE_DSN='<operator secret supplied outside source control>'
python -m nano_aural_runtime.durable.migration_admin \
  --adopt-checksums /absolute/private/trusted-migration-checksums.json
python -m nano_aural_runtime.durable.migration_admin --verify
python -m nano_aural_runtime.durable.service --migrate-only
python -m nano_aural_runtime.durable.migration_admin \
  --grant-runtime-role nano_aural_runtime
python -m nano_aural_runtime.durable.migration_admin --verify
```

The admin command emits fixed result or error codes. It never prints the DSN,
manifest path, SQL bytes, exception text, or secret values.

The runtime grant is intentionally post-migration. PostgreSQL initialization
must already have created the fixed `nano_aural_runtime` LOGIN with an
independent password and no elevated attributes. The owner-only command grants
application DML and sequence access, establishes owner default privileges for
future application tables, revokes every table/column ledger DML privilege,
grants ledger SELECT only, and reruns exact seal verification. It neither
creates the role nor accepts an arbitrary identifier.

## Recovery-set contract

Run the API, workers, reapers, upload verification, and any other database or
canonical-store writer down for the whole drill. From the initial empty-target
preflight until completion, the target PostgreSQL database and target
canonical root must be offline and exclusively owned by this operation. This
is a hard safety precondition, not a recommendation.

`pg_dump`, `pg_restore`, and `psql` are selected only from one absolute,
canonical binary directory. Each binary must be a regular executable owned by
the current user or root and not group/world-writable. PostgreSQL credentials
come from an absolute owner-owned `0600` `pg_service.conf`; commands use only
its validated service name. The service file and client binaries are opened
without following symlinks; their descriptors remain identity anchors. Every
call exactly rechecks path and descriptor metadata before and after the child,
also rehashing the service file. Children receive that continuously bound
service file through `/dev/fd`, a minimal
environment, `shell=False`, bounded stdout, fixed timeout, and discarded
stderr. A replaced or in-place modified service/tool fails closed. Command
arguments, stderr, DSNs, and private paths are never relayed in tool output.

A complete recovery-set directory contains exactly:

```text
database.dump
canonical/blobs/sha256/<aa>/<bb>/<full-sha256>
manifest.json
```

`database.dump` is PostgreSQL custom format. `manifest.json` is written last
and records only its SHA-256/size, a SHA-256 pseudonymous source database
identity, plus the sorted canonical object key, SHA-256, size inventory and
total bytes. The identity hashes the server-reported cluster system identifier
and database OID/name; it lets restart reject a switched service or database
without exposing those values. The manifest contains no DSN, service path,
namespace, job/attempt/artifact id, request, token, host, or source path.
Every file is exactly `0600`; directories are exactly `0700`. Files and all
nested directories are fsynced before staged publication. Limits independently
bound dump bytes, object count, one canonical object, total canonical bytes,
temporary staging bytes, manifest bytes, subprocess output, and subprocess
time. For backup, temporary bytes means dump plus canonical stage bytes; for
restore, it means the newly created canonical stage (the already-existing,
read-only recovery-set dump is not counted a second time). All hashing and
copying is chunked; file growth past a declared or configured bound aborts.

Both source roots and targets must be absolute, canonical, private,
non-symlink directories, and source/target trees must be disjoint. Canonical
keys must exactly match `blobs/sha256/aa/bb/<digest>`. Unknown files,
unlisted or empty prefix directories, symlinks, alternate spellings, digest
drift, inventory drift, and existing destinations fail closed.

### Create a recovery set

```sh
python -m nano_aural_runtime.durable.release_recovery backup \
  --postgres-bin-root /absolute/postgresql/bin \
  --pg-service-file /absolute/private/pg_service.conf \
  --service source \
  --canonical-root /absolute/private/canonical-source \
  --recovery-set /absolute/private/recovery-set-001 \
  --max-single-blob-bytes 68719476736 \
  --max-blob-bytes 1099511627776 \
  --max-dump-bytes 1099511627776 \
  --max-temporary-bytes 2199023255552
```

Before starting the dump, backup requires full release authority: the exact
ordered filename/SHA-256 ledger must equal the migrations packaged with the
running tool, in addition to passing every seal and ACL check. A wrong digest,
historical prefix, or unknown row fails before `pg_dump`.

Backup uses a deterministic private sibling stage and a fixed bounded state
file. The dump, canonical inventory, hashes, manifest, and directory fsyncs
complete before the stage is atomically renamed with a kernel no-replace
operation to the final path. Persistent state binds the server-reported source
database identity. A crash with an incomplete build causes the next identical
command to discard only its marked private stage and rebuild; if a complete,
verified manifest was already durable, it resumes publication without a
second dump. The resume path recursively fsyncs the complete staged tree again
before advancing the durable state; an interrupted fsync leaves `building` in
place and the next restart repeats it. A crash after final rename also
converges without overwriting.
A switched source service/database is rejected before dump or publication.

### Restore into an empty target

Configure another service in the same owner-only file for a newly created,
empty, offline target database, then run:

```sh
python -m nano_aural_runtime.durable.release_recovery restore \
  --postgres-bin-root /absolute/postgresql/bin \
  --pg-service-file /absolute/private/pg_service.conf \
  --service empty-target \
  --recovery-set /absolute/private/recovery-set-001 \
  --canonical-root /absolute/private/canonical-restored
```

Restore first verifies the entire dump and canonical inventory, copies blobs
into a private sibling stage with per-object, aggregate, and temporary-byte
bounds, rehashes them, and fsyncs the staged tree. It
then writes `database_restoring` before invoking `pg_restore` with
`--single-transaction --exit-on-error --no-owner --no-privileges`.

Persistent restore state stores the target's server-reported cluster/database
identity and rechecks it before database restore and canonical publication; a
switched target is rejected. Under the mandatory exclusive/offline target rule, restart interpretation is
unambiguous: an empty database while `database_restoring` means the single
transaction did not commit and may be retried; a non-empty database means the
single transaction committed before the local marker was advanced. Before
canonical publication, the authority probe is fixed to `pg_catalog` and
`public` and requires the exact ordered packaged filename/SHA-256 migration
set, exact table and column ACL boundary, exact validated CHECK, exact
trigger/function definition, and enabled `ORIGIN` or `ALWAYS` mode. Shadow
objects, old prefixes, wrong/unknown rows, no-op functions, unexpected DML
grants, disabled triggers, and replica-only triggers fail. A real drill still
calls the full migration verifier as an independent post-restore check.

After database authority is verified, the canonical stage is renamed to the
previously absent target. A crash after database restore or canonical rename
resumes from the state file and never runs a second committed restore or
overwrites a target. If any external actor could have written the target DB,
the non-empty resume inference is unsafe: quarantine that target, create a new
empty offline database and absent canonical root, and repeat the drill.

The successful CLI output contains bounded counts only. It does not identify
objects, rows, paths, credentials, or operators.

## Real drill verification

Use PostgreSQL 16 client tools from the same distribution as the server. The
automated real drill creates a VERIFIED input, job, events, fenced attempt,
FINALIZED publication, SUCCEEDED job, READY visible artifact, and canonical
input/output bytes. After dump and restore it verifies:

1. the complete migration ledger against packaged SQL bytes;
2. job events are readable;
3. the visible winner/catalog join returns exactly one READY winner;
4. winner SHA-256 and size equal publication evidence;
5. downloaded canonical output bytes match both values;
6. the verified input canonical bytes also match.

Run the focused suite with:

```sh
NANO_AURAL_POSTGRES_BIN=/absolute/postgresql/bin \
  pytest -q tests/test_release_migration_recovery.py
```

If `pg_dump`, `pg_restore`, or `psql` is absent, the real drill reports
`UNRUN_ENV`. Do not translate that skip into a pass. In CI, include this whole
test file in the PostgreSQL job and use a binary directory containing all six
required server/client executables.
