"""Generic nano-aural CLI dispatcher. Help and routing import no model backends."""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from nano_aural_runtime_workers.plugins import DEFAULT_PLUGIN_CATALOG


def _usage() -> str:
    lines = [
        "usage: nano-aural <adapter> ...",
        "",
        "Adapters:",
    ]
    for plugin in DEFAULT_PLUGIN_CATALOG.all_plugins():
        status = "installed" if plugin.implemented else "not installed"
        lines.append("  {0:16} {1} ({2})".format(plugin.frontend, plugin.adapter_id, status))
    lines.append("")
    lines.append("Compatibility alias: nano-aural-controlfoley")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("-h", "--help"):
        sys.stdout.write(_usage())
        return 0
    frontend = arguments[0]
    if frontend in ("-V", "--version"):
        sys.stdout.write("nano-aural 0.1.0.dev0\n")
        return 0
    try:
        plugin = DEFAULT_PLUGIN_CATALOG.get_frontend(frontend)
    except KeyError:
        sys.stderr.write("nano-aural: unknown adapter frontend\n")
        sys.stderr.write(_usage())
        return 2
    if not plugin.implemented:
        sys.stderr.write("nano-aural: adapter frontend is not installed\n")
        return 2
    if plugin.frontend == "controlfoley":
        from nano_aural_runtime_controlfoley.cli import main as controlfoley_main

        try:
            return controlfoley_main(arguments)
        except SystemExit as error:
            if error.code is None:
                return 0
            if isinstance(error.code, int):
                return error.code
            return 2
    sys.stderr.write("nano-aural: adapter frontend is not installed\n")
    return 2


def controlfoley_alias(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "controlfoley":
        arguments = ["controlfoley", *arguments]
    return main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
