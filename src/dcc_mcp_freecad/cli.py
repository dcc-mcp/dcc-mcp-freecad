"""Public command line for the FreeCAD standalone adapter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .__version__ import __version__
from .doctor import doctor_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dcc-mcp-freecad")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    for verb in ("doctor", "verify"):
        command = subparsers.add_parser(verb)
        command.add_argument("--json", action="store_true", dest="as_json")
        command.add_argument("--dcc-path", type=Path)
        command.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from .server import main as server_main

        server_main()
        return 0
    args = _parser().parse_args(arguments)
    if args.command is None:
        _parser().error("a command is required")
    report = doctor_report(args.dcc_path, args.command, args.timeout)
    exit_code = int(report.pop("_exit_code"))
    if args.as_json:
        print(json.dumps(report, sort_keys=True))
    else:
        print("%s: %s" % (args.command, report["status"]))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
