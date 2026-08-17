# nanoAuralRuntime

nanoAuralRuntime is an audio-native, model-agnostic Runtime Core, adapter SDK,
and durable serving stack. ControlFoley is its first adapter and vertical
workflow; it is not the system boundary.

Status: active pre-alpha. The software/CPU phases are implemented, but this is
not a completed release. The required RTX 4090 parity, benchmark, worker,
remote, and UI evidence remains **DEFERRED** and release-blocking. No parity,
quality, acceleration, or performance claim is made from skipped tests.

## Architecture and authority

Dependencies point down through the fixed layers:

```text
Frontend -> Workflow -> Durable Service / Local Executor
         -> Runtime Core -> Model Adapter -> Original Model Backend
```

The Runtime Core has no ControlFoley, PostgreSQL, storage, API, or ComfyUI
dependency. ComfyUI is an optional removable frontend, and the durable database
remains the authority for asset, job, attempt, and artifact state.

Authoritative project documents:

- [Project charter](PROJECT_CHARTER.md)
- [Architecture](ARCHITECTURE.md)
- [Roadmap](ROADMAP.md)
- [Phase status and gates](plans/STATUS.md)
- [ADR 0003: isolated Worker environments](docs/decisions/0003-model-specific-worker-environments.md)
- [ADR 0004: adapter plugin and Worker routing](docs/decisions/0004-adapter-plugin-and-worker-routing.md)

## Install an audited development artifact

Build the headless Python artifacts offline with the pinned setuptools 82.0.1
backend. The command creates an isolated source snapshot, accepts only the four
declared Python package trees and five migration resources, audits every wheel
and sdist member, refuses overwrite, and reports artifact SHA-256 values.

```sh
mkdir -p dist
python tools/release_artifacts.py --output-dir dist
python -m pip install --no-index --no-deps \
  dist/nano_aural_runtime-0.1.0.dev0-py3-none-any.whl
```

The base distribution has no mandatory third-party dependency. PostgreSQL
runtime support is the explicit `durable-postgres` extra; test and development
dependencies remain separate.

```sh
python -m pip install '.[durable-postgres]'
python -m pip install -e '.[dev,postgres-test]'
```

ControlFoley source, dependencies, weights, checkpoints, fixtures, and caches
are operator-supplied external materials. They are not installed by any extra
and are not included in a wheel, sdist, optional frontend archive, or reference
container. The official ControlFoley repository identifies its source code as
[Apache-2.0](https://github.com/xiaomi-research/controlfoley), while its official
model card identifies the weights as
[CC BY-NC 4.0](https://huggingface.co/YJX-Xiaomi/ControlFoley/blob/main/README.md),
which includes a NonCommercial restriction. This project grants no rights to
those external materials. Operators must verify the license at their exact
pinned source revision and the current model-card terms at acquisition and use;
see [NOTICE](NOTICE).

## Headless commands

Help must work without model weights, API credentials, PostgreSQL, or network
access:

```sh
nano-aural --help
nano-aural-remote --help
python -m nano_aural_runtime_remote --help
python -m nano_aural_runtime.durable.service --help
python -m nano_aural_runtime.durable.reference_worker --help
python -m nano_aural_runtime.durable.recovery --help
```

`nano-aural controlfoley local` is the local operator-controlled path.
`nano-aural-remote` exchanges verified asset, job, and artifact identifiers
with the public durable API. Consult the command help and the
[durable operations guide](docs/durable-operations.md) before supplying
deployment or service configuration.

## Optional ComfyUI source archives

The main wheel intentionally contains no `integrations` package. It is not a
silent omission: the three optional source trees have separate, removable,
deterministic release carriers.

```sh
mkdir -p dist/comfyui
python tools/release_comfyui_archives.py --output-dir dist/comfyui
```

This produces independently versioned Embedded, Remote, and Compat zip files.
Each contains one importable custom-node-style package, `LICENSE`, `NOTICE`,
and `RELEASE-MANIFEST.json` with the exact member SHA-256 and size. Extract only
the frontend required by the host. Removing any or all archives leaves the
headless wheel unaffected. See the
[compatibility and removal guide](docs/comfyui-compatibility-removal.md).

## Durable reference environment boundary

`compose.yaml` and `ops/Dockerfile.api` are a CPU fake-publication reference
environment. The image copies only `nano_aural_runtime`, runs as a non-root user,
uses mounted secret files, and excludes ControlFoley, torch, CUDA, weights, and
ComfyUI. The reference build is explicitly `linux/amd64`; its Python base uses
a tag plus digest and its resolved Python dependencies use exact wheel hashes
with binary-only installation. Its standard-library WSGI server is not a
production HTTP/TLS server. Production exposure requires an independently
supported reverse proxy/server, TLS, request timeouts, concurrency limits, and
an operational security review.

Docker daemon validation is environment-conditional and remains unrun when no
container CLI/daemon is available. The inert `gpu-deferred` profile is not a GPU
worker and cannot satisfy a hardware Gate.

## Release and security

- [Release readiness and artifact contract](docs/release-readiness.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)
- [License](LICENSE) and [notices](NOTICE)

Release artifacts must not contain weights, media, caches, secrets, private
paths, generated evidence, or archived research. Report security issues through
the private process in `SECURITY.md`, never through a public issue containing a
credential or private deployment detail.
