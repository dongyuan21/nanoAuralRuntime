# Changelog

All notable changes are recorded here. The project follows semantic versioning
once a release version is declared; the current `0.1.0.dev0` identifier is a
pre-release development snapshot, not a completed release.

## Unreleased

### Added

- A model-agnostic Runtime Core with explicit lifecycle, cancellation,
  single-flight execution, and generic profile/cache reports.
- A sealed ControlFoley adapter/local CLI and explicitly non-default staged,
  profiling, L0/L1, and L2 condition-cache experiments.
- Durable verified uploads, fenced attempts, verified publication, authorized
  download, remote client/CLI, CPU reference worker, recovery commands, and a
  PostgreSQL 16 reference schema.
- Optional Embedded and Remote ComfyUI frontends plus coexistence/removal
  diagnostics.
- Strict offline wheel/sdist construction, deterministic independent ComfyUI
  archives, fresh-install release tests, license/notice material, and pinned CI
  action definitions.
- Checksum-sealed migrations, least-privilege runtime database credentials, and
  restartable PostgreSQL/canonical-object backup and restore with a real
  PostgreSQL 16 recovery drill.

### Security

- Release archives use explicit file allowlists, reject symlinks and unsafe
  member paths, refuse overwrite, and report full-file SHA-256 values.
- Reference container images use reviewed tag-plus-digest identities; the
  Linux/amd64 API dependency closure uses exact versions, reviewed wheel
  SHA-256 values, binary-only resolution, and hash-required installation.
- Credentials, model weights, private fixtures, generated media, caches, and
  benchmark evidence are excluded from published artifacts.

### Deferred release evidence

- RTX 4090 baseline/parity, durable worker/remote closed loops, experimental
  benchmark measurements, and Embedded/Remote ComfyUI UI smokes remain
  **DEFERRED** and release-blocking.
- Real Docker daemon validation is environment-blocked when a container CLI and
  daemon are unavailable.

No parity, quality, speedup, or production-readiness claim is attached to this
development snapshot.
