# Release readiness and artifact contract

## Current decision

Phase 6 non-hardware hardening has passed its independently reviewed software
Gate. This document defines build and validation mechanics; it does not declare
the overall release Gate passed. The required RTX 4090 and real ComfyUI evidence
is still **DEFERRED** and release-blocking. Real PostgreSQL 16 backup/restore has
been executed; Docker daemon validation remains **UNRUN** on this host and must
not be inferred from static tests.

## Headless distribution allowlist

`tools/release_artifacts.py` requires the active interpreter to provide exactly
setuptools 82.0.1, matching the build-system pin, and then runs that backend
without build isolation or network access. Reproducibility evidence applies to
this pinned backend rather than to a range of setuptools implementations. The
tool copies a temporary source snapshot containing only:

- `pyproject.toml`, `README.md`, `LICENSE`, and `NOTICE`;
- Python files beneath `nano_aural_runtime`,
  `nano_aural_runtime_cli`, `nano_aural_runtime_controlfoley`,
  `nano_aural_runtime_remote`, `nano_aural_runtime_stable_audio_3`, and
  `nano_aural_runtime_workers`;
- the five declared `nano_aural_runtime/durable/sql/*.sql` migration resources.

Unexpected package files, unknown migration resources, symlinks, special files,
unsafe archive paths, duplicate wheel members, unexpected setuptools metadata,
or a pre-existing output cause a fail-closed build. Wheel auditing verifies
every `RECORD` row, member hash and size, its empty self-entry, exact console
entry points, and metadata/dependency semantics against `pyproject.toml`.
Sdist auditing rejects duplicate names, binds all four root metadata files to
repository bytes, and requires both `PKG-INFO` copies and all egg-info semantics
to match the audited wheel and project contract.

The completed wheel and sdist are audited before publication. Both destination
names are preflighted before either is created; staging files and final links
are synchronized as one collection. A link or directory-sync failure removes
only targets created by that invocation, preserves unrelated or pre-existing
files, and synchronizes the rollback. Setuptools' sdist content is rewritten in
sorted order with a fixed
timestamp, owner, group, and mode inside a fixed-time gzip envelope; the wheel
uses the same fixed source epoch. Two builds from identical bytes must produce
identical wheel and sdist bytes. The command emits only artifact basenames,
SHA-256, sizes, and an explicit blocked/deferred status—never a release or
hardware claim.

Neither artifact contains `integrations`, tests, benchmarks, archived research,
weights, checkpoints, model/Hugging Face caches, media, secrets, generated
evidence, or repository-local build/cache state.

## Fresh-install acceptance

For every advertised Python version, create a new virtual environment and
install the audited wheel with `--no-index --no-deps`. Acceptance requires:

1. `nano-aural --help` and `nano-aural-remote --help` execute without operator
   configuration or third-party model dependencies.
2. the local adapter, public remote client, durable service/worker/recovery help,
   and all four headless package families import without ComfyUI or torch;
3. `importlib.resources` exposes exactly the five SQL migrations;
4. `integrations.comfyui*` is absent;
5. the installed distribution includes the Apache `LICENSE`, `NOTICE`, and two
   declared console entry points.

The base wheel has no mandatory third-party dependency. PostgreSQL support is
tested separately using `.[durable-postgres]` or `.[postgres-test]`. The
ControlFoley backend and model material remain operator-managed external
dependencies rather than a Python extra.

## Optional ComfyUI carriers

`tools/release_comfyui_archives.py` builds exactly three independent archives:

- `nano-aural-comfyui-embedded-<version>.zip`;
- `nano-aural-comfyui-remote-<version>.zip`;
- `nano-aural-comfyui-compat-<version>.zip`.

Each archive has one distinct import/package root, exact source-file allowlist,
fixed timestamp and Unix mode, stored bytes with no compressor variance, and a
canonical `RELEASE-MANIFEST.json`. The manifest binds distribution/version,
every member SHA-256/size, and explicit statements that ControlFoley source,
weights, and hardware evidence are absent. `LICENSE` and `NOTICE` travel with
every carrier. All three destinations are preflighted and published as one
failure-atomic, directory-synchronized collection. Building twice from
identical bytes must produce identical zip bytes.

These archives are source carriers, not PyPI dependencies and not business-state
authorities. Embedded still requires the installed headless wheel plus sealed
operator source/weight configuration. Remote requires the installed headless
wheel and public service configuration but no model package. Compat is optional
diagnostic support. Any or all can be deleted without changing the headless
installation.

## Container and HTTP boundary

The reference Dockerfile intentionally copies only `src/nano_aural_runtime`,
while the audited wheel contains that exact tree plus separately layered local
adapter/worker/client packages. Packaging tests prove the wheel's Core/durable
bytes equal the Docker build-context source. The `durable-postgres` library
extra remains the compatible range `psycopg[binary]>=3.1,<4`; the separate
container requirements file is its Python 3.12/Linux x86_64 resolved subset,
with exact versions, reviewed wheel SHA-256 values, `--require-hashes`, and
binary-only resolution. Compose fixes every service built from the Dockerfile
to `linux/amd64`, and the Python base tag is pinned to a reviewed Docker Hub
index digest. This does not claim other architectures. The Docker image is not
a substitute for the general wheel, and a source-copy image must be rebuilt
from the same reviewed revision as its wheel artifacts.

The standard-library WSGI server and Compose topology are CPU recovery/reference
tools. They do not provide a production TLS, proxy, multi-process, slow-client,
or Internet-edge security boundary. A production claim requires a separately
reviewed server/proxy deployment, timeouts, concurrency/resource limits,
graceful shutdown evidence, backup/restore drill, and real daemon validation.

## CI evidence and conditional environments

The clean-checkout workflow pins third-party actions to commit SHAs and is
configured to run:

- Python 3.9–3.12 CPU tests plus per-version wheel/sdist/fresh-venv packaging;
- Ruff format/check and Pyright across source, optional integrations, tools, and
  tests;
- PostgreSQL 16 migration, service loopback, and publication suites when the
  runner exposes the declared PG16 binaries;
- a separate, explicitly opted-in Docker reference job;
- a separate, explicitly opted-in self-hosted RTX 4090 evidence-collection job.

The PostgreSQL 16 service image used by both Compose and CI retains its readable
`postgres:16.3-bookworm` tag and pins the reviewed Docker Hub index digest.
Static audit coverage rejects missing or malformed digests in these formal
`image` fields. This is declaration evidence, not a registry or daemon check.

Docker and GPU jobs are not silently converted to success when unavailable.
The GPU job is evidence collection only: output still requires independent
review against every Roadmap hardware Gate. A skipped test is never a passing
hardware result.

## Outstanding release blockers

- Complete and independently review every deferred RTX 4090 runbook/evidence
  set, including real Embedded and Remote ComfyUI host execution.
- Execute the Docker job on a real daemon and record startup, migration, fake
  closed loop, restart, secret/log, volume, and shutdown evidence.
- Repeat the successful wheel/sdist, SPDX validation, API-lock vulnerability
  audit, CycloneDX generation, secret scan, dependency/license review, and
  artifact checksum capture from the exact approved release revision. The
  current evidence belongs to the `0.1.0.dev0` pre-alpha candidate.
- Resolve or explicitly accept every remaining final-candidate finding,
  including archive identity for range-based library/build declarations and
  any `NOASSERTION` license field, before making a release claim.
- Replace `0.1.0.dev0` only when the candidate version and release notes are
  approved. Do not remove the pre-alpha or experimental wording before then.
