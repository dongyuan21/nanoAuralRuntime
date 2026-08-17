# Durable service operations and recovery

## Evidence boundary

This is the Roadmap Phase 3E CPU reference environment. PostgreSQL is the
authority for assets, jobs, attempts, leases, publication state, READY
artifacts, and the winning attempt. The canonical BlobStore is the authority
for bytes only after database verification. Attempt objects are temporary,
immutable publication intermediates.

The included worker is the Core `FakeAudioAdapter` producing a deterministic
silent WAV. It proves queue, Runtime, fencing, validation, publication, and
download recovery without torch, CUDA, ControlFoley, model weights, or
ComfyUI. It is not a model-quality validation. The full ControlFoley remote
closed loop and 4090 smoke/recovery remain **DEFERRED** and are not represented
as passing here.

## Compose reference startup

`compose.yaml` is JSON-form YAML so both Compose and standard JSON tooling can
parse the checked-in reference. It defines PostgreSQL 16, one-shot migration
and runtime-privilege services, the API, the CPU fake publication worker,
persistent database/canonical/staging/attempt volumes, and an inert
`gpu-deferred` profile. Every service built from `ops/Dockerfile.api` is fixed
to `linux/amd64`. The Python 3.12 base uses a tag plus reviewed Docker Hub index
digest, and the API dependencies are an exact, hash-required, binary-only
Linux x86_64 lock. Running this reference on another host architecture therefore
requires amd64 support or emulation; the checked-in hashes do not claim native
arm64 or ppc64le support. The separate `postgres:16.3-bookworm` Compose image
and matching CI service retain that tag and pin the reviewed Docker Hub index
digest `sha256:d0f363f8366fbc3f52d172c6e76bc27151c3d643b870e1062b4e8bfe65baf609`.

Prepare five distinct owner-only (`0400` or `0600`) secret files outside the repository. The
migration and runtime credentials must never be the same secret or password:

| Secret | Content |
| --- | --- |
| `NANO_AURAL_POSTGRES_MIGRATION_PASSWORD_FILE` | Password for the fixed database owner `nano_aural_migrator`. |
| `NANO_AURAL_POSTGRES_RUNTIME_PASSWORD_FILE` | Independent password used only to initialize the non-owner `nano_aural_runtime` LOGIN. |
| `NANO_AURAL_MIGRATION_DATABASE_DSN_FILE` | Owner DSN for `nano_aural_migrator`; mounted only by migration and privilege services. |
| `NANO_AURAL_RUNTIME_DATABASE_DSN_FILE` | Non-owner DSN for `nano_aural_runtime`; mounted only by API and workers. |
| `NANO_AURAL_TOKEN_GRANTS_FILE` | JSON array of `token_sha256`, `subject`, `scopes`, and `namespaces`; never plaintext bearer tokens. |

Set those five environment variables to absolute secret-file paths, then run:

```sh
docker compose config
docker compose up --build --wait \
  postgres migrate grant-runtime-privileges api cpu-reference-worker
```

The migration service runs `python -m nano_aural_runtime.durable.service
--migrate-only` as the migration owner; repeated execution is a ledgered
no-op. Only after it completes, `grant-runtime-privileges` runs
`migration_admin --grant-runtime-role nano_aural_runtime` with the owner DSN.
It validates the fixed role is a non-owner LOGIN with no superuser, role,
database, replication, inheritance, bypass-RLS, or membership elevation. It
then grants schema usage, application-table DML, sequence access, and matching
owner default privileges for future tables. The migration ledger is SELECT
only: table and column DML grants are revoked and the exact seal is reverified.
The operation is idempotent.

For file-backed secrets, Compose cannot remap host ownership. A root wrapper
therefore accepts only a regular, non-symlink, owner-only source, copies at
most 4 KiB into a private PostgreSQL-owned `tmpfs` file with mode `0400`, and
immediately hands control to the image's official entrypoint. PostgreSQL
initialization reads only that staged file without putting the password in
process arguments. The SQL uses a psql variable literal and fixed role
identifier. API and worker services
depend on successful privilege completion and mount only the runtime DSN;
they never receive the migration password or DSN. Normal API startup does not
implicitly migrate. The CPU reference deployment id is
`00000000-0000-4000-8000-000000000301`; submit only jobs requiring the single
`output` artifact kind to this fake deployment.

The API image copies only `src/nano_aural_runtime`. It excludes the
ControlFoley package, torch/CUDA dependencies, weights, upstream sources, and
ComfyUI. Enabling `--profile gpu-deferred` starts only an intentional failing
placeholder; there is no checked-in GPU image or GPU readiness claim.

## Process and restart rules

- PostgreSQL must be healthy and both one-shot migration and runtime privilege
  services successful before API or worker startup.
- API and worker connections use autocommit for reads; every durable mutation
  still owns an explicit repository transaction.
- Runtime and publication heartbeat monitors use independent PostgreSQL
  connections and close them after each attempt. The worker unloads its Core
  session and closes stores and the command connection on graceful shutdown.
- Database restart is safe after PostgreSQL recovery. Restart migration, API,
  and worker; the migration ledger makes this idempotent.
- API restart loses no durable state. A request interrupted before commit may
  be retried with the same idempotency key; download clients verify full-file
  SHA-256 and size.
- Worker restart never assumes its old attempt. Wait for lease expiry, run the
  reaper, then allow a fresh claim with a higher epoch.
- Object-store/local-volume restart must preserve canonical, staging, and
  attempt volumes. Do not start workers if canonical storage is unavailable.

## Recovery commands

API, worker, and routine recovery commands consume the runtime DSN through
`ops/load_secrets.py`; schema migration and privilege administration consume
the separate migration DSN. The original `NANO_AURAL_DATABASE_DSN_FILE`
loader input remains available only for non-Compose deployments. Commands
print only bounded structured outcomes and counts, never job, attempt,
artifact, prompt, path, token, or DSN values.

```sh
# Inspect help without a database or secrets.
python -m nano_aural_runtime.durable.reference_worker --help
python -m nano_aural_runtime.durable.recovery --help

# Reap expired leases using PostgreSQL's clock.
docker compose run --rm cpu-reference-worker \
  python -m nano_aural_runtime.durable.recovery --reap-expired

# Expire DB-clock-overdue uploads and delete only terminal staging bytes.
docker compose run --rm api \
  python -m nano_aural_runtime.durable.recovery --expire-uploads

# Mandatory dry run before attempt-object deletion; the grace floor is 5 min.
docker compose run --rm cpu-reference-worker \
  python -m nano_aural_runtime.durable.recovery \
  --attempt-orphans-dry-run --grace-seconds 600 --limit 100

# After reviewing database state and backups, perform bounded deletion.
docker compose run --rm cpu-reference-worker \
  python -m nano_aural_runtime.durable.recovery \
  --sweep-attempt-orphans --grace-seconds 600 --limit 100
```

The orphan sweeper deletes only attempt-specific objects authorized by the
publication ledger or old inventory objects with no ledger row. Active leases
and the first stale-grace observation are retained. Run it again after the
database-enforced stale grace if the first pass only marks a publication
stale. There is deliberately no canonical-blob deletion command: canonical
orphans created by a crash after promotion are safe, deduplicated retention.
Reclaim/deletion requires a later independently reviewed reachability policy.

Attempt stores record a bounded inventory journal before creating an immutable
attempt object. Dry-run and sweep have independent persistent cursors on the
attempt volume. `--limit N` bounds journal entries inspected, objects hashed,
database key arrays, and recovery candidates per invocation. Known/active or
young entries consume the current page but advance its cursor, so repeated
runs reach later unknown objects instead of repeatedly scanning one prefix.

`--expire-uploads --limit N` applies one global bound across both DB-clock
expiry transitions and terminal staging-object cleanup. Every inspected
terminal row records idempotent cleanup evidence in PostgreSQL, including when
its staging object was already absent. This prevents an empty early page from
starving later objects; repeat bounded runs until the reported count is zero.
An unexpired `VERIFYING` session is never a terminal cleanup candidate.

## Recovery matrix

| Failure or observation | Durable evidence | Operator action | Safe result |
| --- | --- | --- | --- |
| API/DB disconnect before job commit | No job, or the already committed idempotent job | Restore DB/API; resubmit the identical body and idempotency key | One job id, or a conflict for different content |
| Worker process exits while lease active | RUNNING job, ACTIVE attempt, worker BUSY until DB lease expiry | Do not reset rows manually; after expiry run `--reap-expired`, restart worker | Old epoch is fenced; retry gets a higher epoch |
| Cancellation races execution/publication | `cancel_requested_at` fences heartbeat/publication/finalize | Let monitor cancel or reaper terminalize; retry cancel/status safely | No cancelled attempt can become visible winner |
| Reaper races heartbeat | PostgreSQL clock and worker→job→attempt locks decide | Run bounded reaper again if needed | Exactly one current executor; stale heartbeat/finalize fails |
| Upload stuck `VERIFYING` before expiry | Staging bytes plus nonterminal session | Preserve staging; a verifier owning the exact version may reclaim. Otherwise wait for DB expiry and run `--expire-uploads` | Only a fully re-read SHA/media-verified asset becomes VERIFIED |
| Upload expires in `INITIATED`, `UPLOADED`, or `VERIFYING` | DB-clock-overdue session | Run `--expire-uploads` | Session becomes terminal EXPIRED and staging bytes are removed |
| `after_attempt_write` crash | RESERVED row; immutable object may exist but its key was not recorded | Restart worker if lease is current; otherwise dry-run/sweep inventory after grace | Replay writes identical bytes or old unledgered attempt object is deleted |
| `after_object_recorded` crash | OBJECT_WRITTEN row with immutable key/SHA/size | Restart current attempt; otherwise reap then sweep after stale grace | Validation resumes from recorded evidence |
| `after_validation` crash | OBJECT_WRITTEN row; object validated in process but no canonical evidence committed | Restart current attempt | Validator re-reads bytes; no visibility exists |
| `after_canonical_promotion` crash | OBJECT_WRITTEN row; canonical blob may exist without DB blob/publication link | Restart current attempt; retain canonical bytes conservatively | Deduplicated promotion resumes; unlinked canonical is never exposed or auto-deleted |
| `after_validated_recorded` crash | VALIDATED row and VERIFIED canonical blob | Restart current attempt before lease expiry, or reap and sweep attempt object after grace | Canonical evidence is rechecked before finalization |
| Cancel/reaper immediately before finalization | VALIDATED publication but stale/cancelled lease | Do not force success; reaper/cancel owns terminal state, then sweep | Finalization CAS rejects stale epoch; no visible artifact |
| `after_finalize` crash | SUCCEEDED job, succeeded attempt, FINALIZED publication, READY artifact, winning attempt | Treat DB result as committed; retry status/download; run attempt cleanup later | Exactly one visible verified winner remains |
| Attempt cleanup crashes after winner commit | Terminal publication with cleanup evidence absent | Dry-run then bounded sweep | Only attempt intermediate is removed; visible canonical artifact remains |
| Canonical object unavailable or integrity differs | DB evidence exists but storage read/stat fails | Stop worker/API download traffic, restore the exact verified bytes from backup, investigate storage | Never replace a canonical key with different bytes; do not mark success manually |
| Full API/worker/object restart | PostgreSQL and named volumes persist | Start Postgres, migrate, API, worker; reap expired attempts and expire uploads | Queued jobs resume; committed winners remain visible |

Never edit `jobs`, `job_attempts`, `artifact_publications`, `artifacts`, or
`upload_sessions` by hand to skip a transition. The constraints and CAS paths
are the recovery mechanism, not an obstacle to it.

## Observability and secret rules

`durable.observability` is the only service logging/metrics policy. Its metric
names and complete label domains are fixed in code:

- `nano_aural_api_requests_total`: route class, method, outcome;
- `nano_aural_publications_total`: fixed publication stage and outcome;
- `nano_aural_lease_events_total`: heartbeat/cancel/lost/reaped/retry and outcome;
- `nano_aural_orphan_actions_total`: retain/abandon/delete and outcome;
- `nano_aural_download_integrity_total`: fixed integrity outcome.

Structured event fields are limited to an allowlisted component, outcome,
reason code, timestamp, and bounded numeric `bytes`, `count`, or
`duration_ms`. Never add namespace, subject, job/attempt/artifact/asset ids,
idempotency keys, prompt/request content, URLs, local paths, object keys,
headers, DSNs, tokens, secret-file paths, or exception strings as labels or
log fields. Diagnose a particular job only through the authenticated API or a
restricted database session.

Bearer plaintext belongs only in the client secret manager. Server grants
contain SHA-256 digests. DSNs and grants enter containers through read-only
Compose secrets and the allowlisted loader; they must not appear in images,
Compose environment literals, `.env`, logs, metrics, crash reports, or shell
history. Rotate a token by adding a new digest, restarting API instances, then
removing the old digest after clients have moved.

## Backup and verification

Back up PostgreSQL and canonical objects as one operational recovery set.
Staging and attempt objects are recoverable intermediates but retaining them
helps diagnose interrupted operations. A restore drill must verify migrations,
job/event reads, winner/catalog joins, and full-file download SHA-256. It must
not report the deferred 4090 validation as passed.
