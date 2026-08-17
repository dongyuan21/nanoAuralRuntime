# Release security and supply-chain evidence

## Scope and non-claims

This document describes the Phase 6 non-hardware release-security slice.  The
audit is deterministic and uses only the Python standard library.  It does not
access the network, install software, build a container, resolve a base-image
digest, query a vulnerability database, or decide the overall release Gate.

The main JSON schema is an internal inventory named
`nano-aural-internal-release-security-evidence`; it must not be presented as a
standard SBOM.  With an explicit reproducible creation epoch, the tool also
embeds and can write a standalone SPDX 2.3 JSON SBOM.  Neither document is
CycloneDX, a vulnerability report, a license opinion, or a hardware result.
The internal inventory's `release_gate` field is always `NOT_EVALUATED`.

Formal CycloneDX generation, external SPDX validation, and external
vulnerability scanning are recorded under `capabilities`.  The audit detects
`cyclonedx-py`, `cyclonedx-bom`, `pyspdxtools`, `spdx-tools`, `pip-audit`,
`trivy`, and `grype` on `PATH`, but never invokes them.  Their status therefore
remains `UNRUN` whether absent or merely available.  An empty tool list must
not be presented as a clean vulnerability result or independent SPDX
validation.

## Generate internal evidence

Run from a clean repository checkout:

```bash
.venv/bin/python tools/release_security_audit.py \
  --root . \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output /absolute/private/path/release-security.json \
  --spdx-output /absolute/private/path/release-security.spdx.json
```

Both outputs are owner-only mode `0600` and are published as one fail-closed
evidence set without overwriting an existing file.  Every target is preflighted
before a temporary is created; a write, link, or file/directory `fsync` failure
removes every output created by that invocation and synchronizes the affected
directory.  `--spdx-output` therefore requires `--output`; standalone stdout
remains available only for the internal document.  The supplied epoch becomes
the SPDX `creationInfo.created` timestamp; no ambient clock or absolute
repository path enters either document.  Omitting the epoch records SPDX
generation as `UNRUN`; it never substitutes an invented creation time.

When packaging produces candidate archives, validate the exact bytes and add
their hashes to the same evidence:

```bash
.venv/bin/python tools/release_security_audit.py \
  --root . \
  --wheel /absolute/path/nano_aural_runtime-0.1.0.dev0-py3-none-any.whl \
  --sdist /absolute/path/nano_aural_runtime-0.1.0.dev0.tar.gz \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output /absolute/private/path/release-security-with-artifacts.json \
  --spdx-output /absolute/private/path/release-security-with-artifacts.spdx.json
```

Supplying no wheel or sdist means those artifact validations are absent, not
passed.

## Evidence contents

`release_inputs` records the project name, declared version, Apache-2.0
license, `LICENSE`/`NOTICE` hashes, and sorted SHA-256/size records for intended
wheel and source inputs.  The container section hashes the Dockerfile and each
local `COPY`/`ADD` input, records base-image name, tag, and digest, and
enumerates static `image` declarations in Compose and the CI workflow.  The
reference image pins `python:3.12-slim-bookworm` to the reviewed Docker Hub
multi-platform index digest
`sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b`.
Both Compose and the PostgreSQL CI service pin `postgres:16.3-bookworm` to
`sha256:d0f363f8366fbc3f52d172c6e76bc27151c3d643b870e1062b4e8bfe65baf609`.
The audit records that declaration but does not resolve or independently verify
the registry object.  A null digest produces
`container.base_image_not_digest_pinned` for a Dockerfile base or
`container.image_not_digest_pinned` for a Compose/CI image; malformed digests
produce the corresponding `*_digest_invalid` finding.  Container build
validation remains `UNRUN` because hashing declarations is not a container
build, registry lookup, or image inspection.

`dependencies` combines build, runtime, optional, and API-container
requirements.  Every row contains the normalized name, declared requirement,
installed status/version/license, installed `METADATA` and `RECORD` hashes,
and a distribution-archive hash field.  The API-container rows come from pip
logical lines, so continuation-line hashes are attached to their package rather
than misread as dependencies.  Its resolved Python 3.12/Linux x86_64 subset is
exactly pinned to `psycopg` 3.2.13, `psycopg-binary` 3.2.13, and
`typing_extensions` 4.15.0.  The lock and Docker install both require hashes
and binary distributions only; Compose fixes every service built from this
Dockerfile to `linux/amd64`.  The single platform-specific binary hash is not
evidence for arm64 or ppc64le support.

A missing installed package or unknown license remains explicit.  Version
ranges and installed metadata hashes are not a lock file and do not prove the
downloaded wheel or sdist identity.  A null `distribution_archive_hash`
produces `dependency.distribution_archive_hash_unavailable` for its declaration
source.  The range-based library extras in `pyproject.toml` intentionally remain
library compatibility declarations; the container lock is their reviewed,
resolved deployment subset.  `supply_chain_findings` also reports missing
`--require-hashes`, missing binary-only policy, unsupported pip options, a
non-exact API-container requirement, a missing or malformed SHA-256 archive
hash, or a base image without a digest.  Each result contains only `file` and
`rule`.

`standard_sbom.document` is a deterministic SPDX 2.3 JSON document.  It
lists the base application, a distinct API-container component, and declared
Python dependencies grouped by normalized name and evidence-backed version.
For library declarations, the version and license come from the observed local
distribution when available.  For an API-container declaration, the version
instead comes from its exact lock and its reviewed wheel SHA-256 is emitted in
the SPDX package `checksums` field; an unrelated installed version cannot
override either value.  Same-name declarations with the same evidence version
share one node, while different versions receive deterministic, versioned,
collision-resistant SPDX identifiers and separate relationship edges.  A
local license is attached to the locked node only when the observed installed
version matches it.  Versioned nodes carry matching purl references.

The document describes both shipped components without
asserting a containment or variant relationship that the Dockerfile does not
prove.  Its graph preserves the declared purpose of each pyproject/container
group using SPDX
`BUILD_DEPENDENCY_OF`, `DEV_DEPENDENCY_OF`, `TEST_DEPENDENCY_OF`,
`OPTIONAL_DEPENDENCY_OF`, and `DEPENDS_ON` relationships.  Base runtime
dependencies attach only to the base application; container-only dependencies
attach only to the API-container component.  An empty base `dependencies` list
therefore produces no base-application `DEPENDS_ON` edge.  Each library package
carries canonical group and requirement declarations.  The namespace
seed covers all package fields, those declarations, the complete relationship
graph, and a SHA-256 binding to the release-input manifest, in addition to the
explicit `SOURCE_DATE_EPOCH`.  A strict built-in validator recomputes that seed
and checks required fields, identifiers, unique references, license allowlists,
timestamps, declaration-to-version sealing, SHA-256 checksum structure, exact
purl versions, and graph closure.  These are standard SPDX 2.3 package and
checksum structures, but `external_validation` remains `UNRUN` until an
independent SPDX validator is actually executed.

`external_materials` explicitly records that the ControlFoley source checkout,
main checkpoint, external weights, Hugging Face cache, model media, and private
fixtures are operator-supplied and excluded from nanoAuralRuntime Python and
container distributions.  This evidence grants no right to obtain, use, or
redistribute them.  Operators must resolve all upstream source, model, dataset,
weight, and dependency licenses separately.

## Artifact validation

The recursive artifact-tree scanner uses an explicit caller-provided top-level
allowlist plus deny rules.  It rejects symlinks, secret files, virtual
environments, VCS/cache directories, model/checkpoint formats, generated media,
private keys, and compiled Python files.

Wheel validation binds the canonical filename to its sole dist-info directory
and to the complete pyproject release contract: Name, Version, Summary, Author,
Requires-Python, License-Expression/License-File, classifiers, project URLs,
extras and Requires-Dist, README body, and dynamic metadata.  It permits exactly
the four declared package roots and the exact seven-file dist-info contract;
even commonly generated but uncontracted members such as `INSTALLER` are
rejected.  It requires all four `__init__.py` files, source-identical
`LICENSE`/`NOTICE`, the two declared console entry points, top-level package
metadata, and the exact build-backend generator/pure-Python tag.  It also rejects
absolute paths, `..`, backslashes, duplicate members, symlinks/special files,
oversized archives/members, unexpected packages or dist-info files,
absent/duplicate required metadata or `RECORD`, unlisted members, malformed
rows, size mismatches, source membership/content drift, and any SHA-256
mismatch.  The `RECORD` self-row must have empty hash and size fields.

Source-distribution validation binds its canonical filename, single root, and
`PKG-INFO` Name/Version; requires source-identical `README.md`, `LICENSE`,
`NOTICE`, `pyproject.toml`, generated `setup.cfg`, and all four package trees.
The exact egg-info member set is mandatory: both `PKG-INFO` copies must equal
the wheel metadata contract; entry points, optional requirements, top-level
packages, dependency links, and `SOURCES.txt` must exactly describe the source
contract.  It rejects additional or changed egg-info content, `tests`,
`integrations`, `setup.py`, other undeclared top levels, absolute/traversing or
backslash paths, duplicate members, links, devices/special files, denied
formats, and oversized archives/members.  Archive validation reads members in
place and never extracts them.  Per-artifact status is
`UNRUN` when no candidate is supplied and `VALIDATED` only after every supplied
archive passes.

## Secret scan and canary

The repository scanner recursively examines bounded UTF-8 text for high-signal
private-key, provider-token, AWS-key, JWT, credential-URL, and hardcoded-secret
patterns.  Output contains exactly `file` and `rule`; it never includes the
matched value, line contents, environment data, or traceback.  The only
built-in exceptions are exact `(repository-relative file, rule, SHA-256 of
matched fixture value)` entries for existing redaction tests.  There are no
substring, path-wide, or rule-wide placeholder exemptions.  Tests verify that
a different credential matching the same rule in the same file remains a
finding.

A release canary is injected through the local and remote CLIs, durable
service configuration/server/close paths, CPU reference worker, and recovery
command error boundaries.  Their public stdout/stderr and any escaped full
traceback are checked, along with the durable HTTP observer log.
Configuration, dependency, filesystem, transport, server-lifecycle, and
unexpected errors at these entrypoints emit only stable generic messages; raw
exception strings, target paths, DSNs, URLs, response bodies, prompts, and
chained causes are not printed.  Existing success and error exit-code classes
are preserved.

## Required external follow-up

The 2026-08-17 non-hardware rehearsal externally validated the generated SPDX
2.3 JSON with `spdx-tools` 0.8.3, resolved the Linux/amd64 API lock with exact
hashes, queried current PyPI vulnerability data with `pip-audit` 2.10.1 (no
known vulnerabilities for the three locked components), and emitted a
CycloneDX JSON for that lock. These are time- and revision-bound observations,
not checked-in release attestations or a container scan.

Before any release claim, operators still need all Roadmap-required hardware
evidence and must repeat independent SPDX validation, CycloneDX generation if
required, and vulnerability scanning against the exact approved candidate and
a current database. They must review every `NOASSERTION`/unknown dependency
license, verify exact built wheel/sdist/container bytes, and resolve or accept
every remaining finding. The checked-in container declarations closing an
internal finding do not validate a registry pull or Docker build. Deferred RTX
4090 checks remain deferred and release-blocking; this non-hardware evidence
does not change their status.
