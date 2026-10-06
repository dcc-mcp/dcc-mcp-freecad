"""The parts skill scripts as an agent meets them: envelopes and error codes.

The scripts live in a hyphenated directory, so they are loaded by path here the
same way the driver loads its sibling modules. What matters is the surface the
caller branches on: a stable ``error`` code for every refusal, and the
read-back evidence carried in context for every insert.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from dcc_mcp_freecad.parts_library import ENV_LIBRARY_ROOTS, PartLibraryError

SCRIPTS = (
    Path(__file__).parents[1] / "src" / "dcc_mcp_freecad" / "skills" / "freecad-parts" / "scripts"
)


def _load(name: str):
    path = SCRIPTS / ("%s.py" % name)
    spec = importlib.util.spec_from_file_location("dcc_mcp_freecad_parts_%s" % name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeBridge:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def list_parts(self, **kwargs):
        self.calls.append(("list_parts", kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def insert_part(self, **kwargs):
        self.calls.append(("insert_part", kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def _patch(monkeypatch, module, bridge):
    monkeypatch.setattr(module, "get_bridge", lambda: bridge)


LISTING = {
    "schema_version": 1,
    "roots": ["/library"],
    "returned": 1,
    "parts": [{"name": "iso4014-m8x40", "category": "fasteners", "path": "fasteners/m8.step"}],
}

INSERTED = {
    "object": {"name": "Bolt", "shape": {"solids": 1, "volume": 120.0}},
    "part_ref": "fasteners/m8.step",
    "verified": ["object.exists", "object.placement"],
}


def test_listing_success_reports_the_scan_as_evidence(monkeypatch):
    module = _load("list_parts")
    bridge = _FakeBridge(result=LISTING)
    _patch(monkeypatch, module, bridge)

    result = module.main(category="fasteners", limit=10)

    assert result["success"] is True
    assert result["context"]["parts"] == LISTING["parts"]
    assert result["context"]["roots"] == ["/library"]
    assert result["postcondition"]["method"] == "library_directory_scan"
    assert result["postcondition"]["verified"] is True
    assert bridge.calls == [("list_parts", {"category": "fasteners", "query": None, "limit": 10})]


def test_listing_failure_names_the_environment_variable(monkeypatch):
    module = _load("list_parts")
    _patch(
        monkeypatch,
        module,
        _FakeBridge(
            error=PartLibraryError(
                "parts_library_unavailable",
                "No parts library is configured.",
                env_var=ENV_LIBRARY_ROOTS,
            )
        ),
    )

    result = module.main()

    assert result["success"] is False
    assert result["error"] == "parts_library_unavailable"
    assert result["context"]["error_code"] == "parts_library_unavailable"
    assert result["context"]["env_var"] == ENV_LIBRARY_ROOTS
    assert ENV_LIBRARY_ROOTS in result["prompt"]


def test_insert_success_keeps_the_read_back_checks_in_context(monkeypatch):
    module = _load("insert_part")
    bridge = _FakeBridge(result=INSERTED)
    _patch(monkeypatch, module, bridge)

    result = module.main(
        document_path="assembly.FCStd", part_ref="fasteners/m8.step", object_name="Bolt"
    )

    assert result["success"] is True
    assert result["context"]["verified_checks"] == ["object.exists", "object.placement"]
    assert result["postcondition"]["verified"] is True
    assert result["context"]["object"]["shape"]["solids"] == 1
    assert bridge.calls[0][1]["translation"] == (0, 0, 0)
    assert bridge.calls[0][1]["rotation_axis"] == (0, 0, 1)
    assert bridge.calls[0][1]["rotation_degrees"] == 0


@pytest.mark.parametrize(
    "code",
    ["invalid_part_ref", "part_ref_escapes_library", "unsupported_part_format", "part_not_found"],
)
def test_insert_refusals_carry_a_stable_code_and_point_back_at_listing(monkeypatch, code: str):
    module = _load("insert_part")
    _patch(
        monkeypatch,
        module,
        _FakeBridge(
            error=PartLibraryError(code, "refused: ../outside.step", part_ref="../outside.step")
        ),
    )

    result = module.main(
        document_path="assembly.FCStd", part_ref="../outside.step", object_name="Intruder"
    )

    assert result["success"] is False
    assert result["error"] == code
    assert result["context"]["part_ref"] == "../outside.step"
    assert "list_parts" in result["prompt"]
