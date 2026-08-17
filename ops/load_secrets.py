"""Load an allowlisted Docker secret into the child process environment."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Optional, Sequence

_SECRET_ENV = {
    "NANO_AURAL_MIGRATION_DATABASE_DSN_FILE": "NANO_AURAL_DATABASE_DSN",
    "NANO_AURAL_RUNTIME_DATABASE_DSN_FILE": "NANO_AURAL_DATABASE_DSN",
    # Non-Compose deployments retain the original single-service-file input.
    "NANO_AURAL_DATABASE_DSN_FILE": "NANO_AURAL_DATABASE_DSN",
    "NANO_AURAL_TOKEN_GRANTS_JSON_FILE": "NANO_AURAL_TOKEN_GRANTS_JSON",
}
_MAX_SECRET_BYTES = 64 * 1024


def _read_secret(path: str) -> str:
    # O_NONBLOCK prevents a hostile/mis-mounted FIFO from hanging the process
    # before fstat can reject every non-regular source.  Regular files retain
    # ordinary read semantics.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(Path(path)), flags)
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode):
            raise ValueError("secret source must be a regular file")
        data = os.read(descriptor, _MAX_SECRET_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > _MAX_SECRET_BYTES:
        raise ValueError("secret source exceeds the bounded size")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("secret source must be UTF-8") from error
    value = value.removesuffix("\n")
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("secret source must contain one non-empty line")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load allowlisted *_FILE secrets and execute a child process."
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    if not options.command:
        parser.error("a child command is required")
    environment = dict(os.environ)
    try:
        for file_name, target_name in _SECRET_ENV.items():
            path = environment.pop(file_name, None)
            if path is None:
                continue
            if target_name in environment:
                raise ValueError("secret target and *_FILE source cannot both be set")
            environment[target_name] = _read_secret(path)
    except (OSError, ValueError):
        sys.stderr.write("secret loading failed; check mounted secret files\n")
        return 2
    os.execvpe(options.command[0], options.command, environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
