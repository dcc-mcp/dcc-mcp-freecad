"""Opt-in isolated interpreter bootstrap for the installed native FreeCAD library.

This launches only the sibling package-owned typed driver. The interpreter and
module directory are operator launch configuration, never MCP tool parameters.
"""

import runpy
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 4:
        raise ValueError("Expected native module directory, request path and result path")
    module_directory, request_path, result_path = sys.argv[1:]
    directory = Path(module_directory).resolve(strict=True)
    if not directory.is_dir() or not any(
        (directory / name).is_file() for name in ("FreeCAD.so", "FreeCAD.pyd")
    ):
        raise ValueError("The configured directory has no native FreeCAD library")
    driver = Path(__file__).with_name("freecad_driver.py").resolve(strict=True)
    sys.path.insert(0, str(directory))
    sys.argv = [str(driver), "--pass", request_path, result_path]
    runpy.run_path(str(driver), run_name="__main__")


if __name__ == "__main__":
    main()
