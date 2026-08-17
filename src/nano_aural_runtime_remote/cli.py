"""Injectable command-line core for the remote client.

Packaging an executable and obtaining credentials are deployment concerns.  A
thin entry point can construct ``RemoteClient`` and call ``run`` without this
module importing an HTTP framework or reading secrets implicitly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence, TextIO

from .client import RemoteClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nano-aural-remote")
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit")
    submit.add_argument("--namespace", required=True)
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--deployment", required=True)
    submit.add_argument("--request-json", required=True)
    submit.add_argument("--input", action="append", default=[], metavar="ROLE=ASSET_ID")
    submit.add_argument("--required-artifact", action="append", default=[])

    for name in ("status", "cancel", "artifacts"):
        command = commands.add_parser(name)
        command.add_argument("job_id")

    events = commands.add_parser("events")
    events.add_argument("job_id")
    events.add_argument("--cursor")
    events.add_argument("--limit", type=int, default=50)

    wait = commands.add_parser("wait")
    wait.add_argument("job_id")
    wait.add_argument("--interval", type=float, default=1.0)
    wait.add_argument("--timeout", type=float)

    download = commands.add_parser("download")
    download.add_argument("job_id")
    download.add_argument("artifact_id")
    download.add_argument("destination", type=Path)

    upload = commands.add_parser("upload")
    upload.add_argument("--namespace", required=True)
    upload.add_argument("source", type=Path)
    return parser


def run(
    argv: Sequence[str],
    client: RemoteClient,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    arguments = build_parser().parse_args(tuple(argv))
    try:
        if arguments.command == "submit":
            request = _request_json(arguments.request_json)
            inputs = [_input(value) for value in arguments.input]
            required = arguments.required_artifact or ["output"]
            result: object = client.submit(
                {
                    "namespace_id": arguments.namespace,
                    "idempotency_key": arguments.idempotency_key,
                    "deployment_id": arguments.deployment,
                    "request": request,
                    "inputs": inputs,
                    "required_artifact_kinds": required,
                }
            )
        elif arguments.command == "status":
            result = client.status(arguments.job_id)
        elif arguments.command == "wait":
            result = client.wait(
                arguments.job_id,
                interval_seconds=arguments.interval,
                timeout_seconds=arguments.timeout,
            )
        elif arguments.command == "cancel":
            result = client.cancel(arguments.job_id)
        elif arguments.command == "events":
            page = client.events(arguments.job_id, cursor=arguments.cursor, limit=arguments.limit)
            result = {"events": page.events, "next_cursor": page.next_cursor}
        elif arguments.command == "artifacts":
            result = {"artifacts": client.artifacts(arguments.job_id)}
        elif arguments.command == "download":
            result = {
                "path": str(
                    client.download(
                        arguments.job_id,
                        arguments.artifact_id,
                        arguments.destination,
                    )
                )
            }
        elif arguments.command == "upload":
            result = client.upload_asset(arguments.namespace, arguments.source)
        else:  # pragma: no cover - argparse enforces the set
            raise AssertionError("unsupported command")
        stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    except Exception:
        # Transport and filesystem exceptions can contain bearer tokens,
        # remote URLs, response bodies, or the complete download target.
        stderr.write("nano-aural-remote: request failed (invalid request object)\n")
        return 2


def _request_json(raw: str) -> Mapping[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("--request-json must be valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("--request-json must contain an object")
    return value


def _input(raw: str) -> Mapping[str, str]:
    if "=" not in raw:
        raise ValueError("--input must use ROLE=ASSET_ID")
    role, asset_id = raw.split("=", 1)
    if not role.strip() or not asset_id.strip():
        raise ValueError("--input role and asset id must be non-empty")
    return {"role": role, "asset_id": asset_id}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Executable wiring is explicit: URL/token come from required environment variables."""
    import os
    import sys

    from .client import UrllibTransport

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if "-h" in arguments or "--help" in arguments:
        # Help is local documentation and must remain usable before credentials
        # or a network endpoint are configured. argparse raises SystemExit(0).
        build_parser().parse_args(arguments)
        return 0
    base_url = os.environ.get("NANO_AURAL_API_URL")
    token = os.environ.get("NANO_AURAL_API_TOKEN")
    if base_url is None or token is None:
        sys.stderr.write("NANO_AURAL_API_URL and NANO_AURAL_API_TOKEN are required\n")
        return 2
    allow_http = os.environ.get("NANO_AURAL_ALLOW_LOOPBACK_HTTP") == "1"
    try:
        client = RemoteClient(
            UrllibTransport(base_url, allow_loopback_http=allow_http),
            token,
        )
    except Exception:
        sys.stderr.write("nano-aural-remote: configuration failed\n")
        return 2
    return run(arguments, client, sys.stdout, sys.stderr)


if __name__ == "__main__":  # pragma: no cover - exercised through the installed script
    raise SystemExit(main())
