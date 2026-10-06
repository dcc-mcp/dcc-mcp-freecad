"""Print the FreeCAD version reported by a driver `system.status` result file."""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit("usage: freecad-version.py <result.json>")
    with open(argv[1], encoding="utf-8") as stream:
        payload = json.load(stream)
    if not payload.get("ok"):
        sys.exit("driver reported: %s" % (payload.get("error") or {}))
    sys.stdout.write(str(payload["result"]["version"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
