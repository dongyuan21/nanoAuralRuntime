# Programme Plan 0003 — Durable Serving (Roadmap Phases 3A–3E)

Status: the programme plan is ready after Phase 1; it contains independent Roadmap Phases 3A–3E, each exactly one PR. Its ControlFoley-backed worker slice additionally needs Phase 2B. PostgreSQL is the authority for jobs, attempts, artifacts, leases, and winning results; BlobStore holds bytes only.

## Scope

Build the verified-asset to durable-job path: LocalFS/S3-compatible BlobStore, PostgreSQL schema/repositories, upload verification with application-level full-file SHA-256, idempotent jobs, `FOR UPDATE SKIP LOCKED` claims, fencing/heartbeat/reaping, worker execution, artifact validation/finalization, HTTP API, and remote CLI.

Every item below is one PR. Preserve at-least-once inference and exactly-one visible winning artifact semantics.

## Non-goals

- No exactly-once GPU execution, arbitrary server file paths, trusting ETag/composite checksum as SHA-256, or making ComfyUI a state authority.
- No cross-request batching, multi-GPU tensor parallelism, cache/profiler optimization, or modification of model sampling.
- No weights/media/secrets/presigned URLs committed.

## Deliverables

### Roadmap Phase 3A (one PR) — contracts, schema, Local BlobStore

Migrations and repositories for deployments, blobs, assets, jobs, inputs, attempts, artifacts, workers, and events; guarded state transitions, idempotency hashing, LocalBlobStore, Fake Runtime Worker, and invariant/property tests. Do not claim real GPU work.

### Roadmap Phase 3B (one PR) — verified upload and content-addressed assets

Upload sessions, direct single/multipart upload contracts, streaming verifier SHA-256, media probe, canonical keys (`blobs/sha256/...`), deduplication, verified-asset guard, staging expiry/janitor, and upload CLI state. Server-generated filenames never become worker paths.

### Roadmap Phase 3C (one PR) — queue, lease, and worker skeleton

Transactional `SKIP LOCKED` claim, monotonic `lease_epoch`, heartbeat CAS, reaper, retry/backoff, worker registration/readiness, cancellation propagation, and materialization with repeat SHA/media verification. Use the fake adapter only.

### Roadmap Phase 3D (one PR) — GPU worker integration

Connect the fenced durable worker to `Runtime.invoke()` and the ControlFoley adapter, with per-attempt workspace, process-fault policy, and one GPU process/session single-flight behavior. It imports no ComfyUI.

### Roadmap Phase 3E (one PR) — artifacts, API, and remote CLI

Validate audio/video and artifact SHA-256, publish attempt-specific immutable objects, finalization CAS/winning attempt, orphan sweeping, authorized download, job/status/cancel/events API, remote CLI, structured logging/metrics, Compose reference environment, and recovery runbook.

## Tests

- Schema constraints/property tests assert: VERIFIED-only inputs, one current attempt, epoch fencing, one winner, `SUCCEEDED ⇒ READY required artifacts`, canonical immutable blob identity, and idempotency conflict behavior.
- Local/S3-compatible BlobStore contract tests; incorrect SHA rejection; multipart ETag is not accepted as content identity; deduplication and worker re-validation.
- Concurrent claim, heartbeat, reaper, stale-worker-finalize, cancellation race, crash-before/after upload/finalization, and orphan tests using the Fake Runtime Worker.
- End-to-end CPU integration: upload → verify → submit → fake worker → READY download, then API/worker/database restart recovery.
- GPU/ControlFoley tests are conditional and must skip without the required 4090 host setup.

## Gate

- A: migrations and fake closed loop prove database invariants and no duplicate job for matching idempotency input.
- B: only VERIFIED assets enter jobs; bad SHA fails; canonical dedup works.
- C: two workers cannot be legal executors; stale workers cannot heartbeat/finalize; terminated attempt is recoverable.
- D: no-ComfyUI worker calls Runtime directly. The required 4090 worker smoke/recovery gate is **DEFERRED** under the explicit hardware deferral, not passed.
- E: exactly one verified visible winner; `SUCCEEDED` has required READY artifacts; CPU recovery matrix passes. The actual full 4090 remote closed-loop remains **DEFERRED** and is not represented as successful.
