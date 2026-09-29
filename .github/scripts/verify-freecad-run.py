"""Fail the real-hardware job when the FreeCAD test did not actually execute.

`pytest -m freecad` exits 0 when its only test is skipped because
`FREECAD_TEST_EXECUTABLE` is unset, which would turn a missing host into a green
run. This guard reads the JUnit report and requires a real, non-skipped pass.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ElementTree


def _as_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit("usage: verify-freecad-run.py <junit.xml>")
    if not os.path.isfile(argv[1]):
        sys.exit("no JUnit report at %s: pytest -m freecad never ran" % argv[1])
    root = ElementTree.parse(argv[1]).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")

    tests = errors = failures = skipped = 0
    for suite in suites:
        tests += _as_int(suite.get("tests"))
        errors += _as_int(suite.get("errors"))
        failures += _as_int(suite.get("failures"))
        skipped += _as_int(suite.get("skipped"))

    if tests == 0:
        sys.exit("no real FreeCAD test was collected; the marker selected nothing")
    if skipped:
        sys.exit("%s of %s real FreeCAD test(s) were skipped; the host did not run" % (skipped, tests))
    if errors or failures:
        sys.exit("%s error(s) and %s failure(s) in the real FreeCAD run" % (errors, failures))

    print("real FreeCAD run executed %s test(s) with 0 skipped" % tests)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
