"""Capability locks executed against a real FreeCAD host.

The contract suite proves the locks hold for the catalog as it is shipped,
with a synthetic host report. This module runs the same locks against a payload
a real FreeCAD actually served, on both supported release lines, because
``host_limited`` cannot be checked without a host: it is a per-version
projection of the compatibility matrix onto the tools that matrix limits.
"""

from __future__ import annotations

import os
from pathlib import Path

import capability_checks
import pytest

from dcc_mcp_freecad import compat
from dcc_mcp_freecad.bridge import FreecadBridge

# The one guarded break in the matrix applies from 1.1 and limits the STL/OBJ
# tessellation path, so the same tool is limited on 1.1 and clean on 1.0.
_GUARDED_BREAK = "meshpart-tessellate-deflection"
_GUARDED_TOOL = "export_geometry"


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_host_capabilities_match_the_tool_catalog(tmp_path: Path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    payload = bridge.capabilities()

    problems = capability_checks.all_problems(payload)
    version = payload["status"]["version"]
    assert problems == [], "FreeCAD %s served drifting capabilities:\n%s" % (
        version,
        "\n".join(problems),
    )

    host_matrix = payload["status"]["host_matrix"]
    limits = payload["host_limited"]
    assert host_matrix["status"] == compat.SUPPORTED
    assert limits["host_version"] == version
    assert limits["matrix_status"] == compat.SUPPORTED

    # A host limit is only useful if it names a tool the catalog declares.
    declared = {tool["name"] for tool in capability_checks.tool_catalog()}
    assert set(limits["tools"]) <= declared, "host_limited names undeclared tools"

    if compat.parse_version(version) >= (1, 1, 0):
        assert limits["tools"].get(_GUARDED_TOOL) == [_GUARDED_BREAK]
    else:
        assert limits["tools"] == {}
