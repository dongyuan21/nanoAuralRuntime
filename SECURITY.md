# Security policy

## Supported versions

nanoAuralRuntime is pre-alpha and has no production-supported release yet. The
current development branch receives security fixes, but it does not carry a
stability, availability, model-quality, or response-time guarantee.

## Report a vulnerability privately

Use the repository host's private security-advisory channel at
<https://github.com/dongyuan21/nanoAuralRuntime/security/advisories/new>. If it
is unavailable, contact the maintainers through an established private project
channel before disclosing details publicly.

Do not place any of the following in a public issue, pull request, log excerpt,
or reproduction archive:

- bearer tokens, token digests, DSNs, passwords, secret-file paths, or headers;
- private source, weight, checkpoint, cache, fixture, or media paths;
- model weights, user media, generated artifacts, or sanitized evidence source
  files that can be linked back to a user or host;
- production namespace, asset, job, attempt, artifact, or storage identifiers.

Provide a minimal description of the affected component/version, impact,
preconditions, and a reproduction using synthetic values. Maintainers may ask
for encrypted evidence through a separate private channel. Rotate any real
credential that was accidentally exposed before continuing the report.

## Security boundaries

- Remote requests accept verified asset identifiers, never server-local paths,
  source directories, weights, Python modules, or arbitrary deployment flags.
- The durable database is the job-state authority; successful model execution
  alone never publishes a result.
- ComfyUI is optional and removable. It is never the execution, job, or artifact
  authority.
- ControlFoley source and model materials are external operator dependencies and
  are not redistributed by this project.
- The checked-in WSGI/Compose stack is a CPU reference environment, not a
  production HTTP/TLS boundary.

See `docs/durable-operations.md` for secret loading, token rotation, recovery,
logging, and backup rules. Security fixes must retain fail-closed cancellation,
integrity validation, fencing, no-overwrite publication, bounded input/output,
and dependency-direction tests.
