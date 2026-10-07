"""Execute one caller-named Python file inside this FreeCAD child process.

This is the in-host half of the adapter's escape hatch. It is intentionally a
separate module from ``freecad_driver.py``: the driver is a strict
method-dispatch table and must stay one, while this entry point runs a file the
adapter did not write. Keeping them apart means the driver's whitelist cannot be
widened by accident when the runner changes.

What this module does *not* do is as deliberate as what it does:

* It does not read source text from anywhere. The only input is a path the
  wrapper already resolved and checked against
  ``DCC_MCP_FREECAD_ALLOWED_ROOTS``; by the time this runs, the file is one the
  operator was able to execute anyway.
* It does not sandbox. The script runs as the operator's account with that
  account's full privileges, and it can import anything that account can
  import. Allowed roots constrain which file may be *named*, not what the file
  may *do* - the wrapper states that plainly so the tool is never read as a
  sandbox.
* It does not load user workbenches, plugins, or macros. The child is launched
  with ``--safe-mode`` and a throwaway user config, so the upstream hangs
  traced to user add-ons (a modal dialog nothing can close, a broken
  FeaturePython object left behind by a previous run) stay unreachable through
  this door, exactly as they are for the typed tools.
* It does not serialise FreeCAD results. There is no document protocol here and
  no result file: the script is responsible for its own persistence, and the
  wrapper reports the process exit code and captured output. A script that wants
  to change a durable document still goes through the typed tools afterwards,
  so the write-after-read contract keeps applying to whatever it left behind.

The module directory is spliced *behind* the standard library and *ahead* of
site-packages rather than inserted at the front. Inserting at ``sys.path[0]``
would let a file named ``json.py`` or ``io.py`` shipped next to a script shadow
a module the runner itself imports, so a script bug would report as an adapter
crash; putting it behind site-packages would let an installed FreeCAD module of
a different ABI win instead. The script's own directory is appended last so the
script can import siblings without those siblings outranking either.
"""

from __future__ import annotations

import os
import sys

SCRIPT_FLAG = "--script"


def _usage() -> str:
    return "usage: script_runner.py [--script] <script.py>"


def _parse_args(argv: list[str]) -> tuple[str, list[str]]:
    """Return ``(module_directory, script_path)`` for either backend's argv.

    The python-module backend is launched as
    ``python -I script_runner.py <module_directory> <script.py>`` because it
    needs the native library location handed to it, while FreeCADCmd is launched
    as ``FreeCADCmd ... script_runner.py --script <script.py>`` because the
    library is already importable there. ``--script`` is accepted but optional in
    the first form so one parser serves both.
    """
    arguments = [item for item in argv[1:] if item != SCRIPT_FLAG]
    if len(arguments) == 2:
        return arguments[0], arguments[1]
    if len(arguments) == 1:
        return "", arguments[0]
    raise SystemExit(_usage())


def _splice_module_path(module_directory: str) -> None:
    """Put the native library directory where it can be found, and no earlier.

    It has to come after the runner's own directory and after the standard
    library - but *before* site-packages, which is where an installed FreeCAD
    module of a different ABI may be waiting to be picked up by mistake. The
    script's own directory is appended last so the script can import siblings
    without those siblings outranking anything the runner needs.

    Getting the order wrong in either direction is a real failure, not a
    theoretical one: too early and a ``json.py`` or ``io.py`` shipped next to a
    script is imported *by the runner* before the script runs, so a script bug
    reports as an adapter crash; too late and the wrong native library wins.
    """
    entries = [entry for entry in sys.path if entry != module_directory]
    insert_at = len(entries)
    for index, entry in enumerate(entries):
        # site-packages is the first directory of the site half; the stdlib and
        # the runner's own directory precede it.
        if "site-packages" in entry or "dist-packages" in entry:
            insert_at = index
            break
    entries[insert_at:insert_at] = [module_directory]
    sys.path[:] = entries


def main(argv: list[str]) -> int:
    module_directory, script_path = _parse_args(argv)
    if module_directory:
        _splice_module_path(module_directory)
    if not os.path.isfile(script_path):
        sys.stderr.write("script runner: no such file: %s\n" % script_path)
        return 2
    with open(script_path, "r", encoding="utf-8") as stream:
        source = stream.read()
    code = compile(source, script_path, "exec")
    # The script's own directory is appended so the script can import siblings.
    # It goes last on purpose: a sibling must not outrank the stdlib or the
    # native library the runner just spliced in above.
    script_directory = os.path.dirname(os.path.abspath(script_path))
    if script_directory not in sys.path:
        sys.path.append(script_directory)
    globals_dict: dict = {
        "__file__": script_path,
        "__name__": "__main__",
        "__builtins__": __builtins__,
    }
    try:
        exec(code, globals_dict)  # noqa: S102 - running the caller's script is the point
    except SystemExit as exit_request:
        code_value = exit_request.code
        if code_value is None:
            return 0
        if isinstance(code_value, int):
            return code_value
        sys.stderr.write("%s\n" % code_value)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
