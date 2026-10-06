"""Bridge-level parts-library behaviour: what reaches the host, and what does not.

The refusal cases matter as much as the happy path: a rejected ``part_ref`` must
cost no FreeCAD process and must leave the document byte-for-byte unchanged.
That is what makes the containment check a gate rather than a warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import FreecadBridge
from dcc_mcp_freecad.parts_library import ENV_LIBRARY_ROOTS, PartLibraryError


class FakeFreecad(FreecadBridge):
    """Records what would have reached the host instead of running it."""

    def __init__(self, root: Path):
        super().__init__(allowed_roots=[root])
        self.executable = "fake-freecadcmd"
        self.calls = []

    def _invoke(self, method, params, timeout_secs=120):
        self.calls.append((method, dict(params), timeout_secs))
        if method == "system.status":
            return {"version": "1.1.4", "python_version": "3.11.14"}
        if method == "document.inspect":
            path = Path(params["document_path"])
            return {
                "file_name": str(path),
                "name": path.stem.replace("-", "_"),
                "label": path.stem,
                "object_count": 1,
            }
        if method == "model.insert_part":
            document = Path(params["document_path"])
            document.write_bytes(document.read_bytes() + b"-mutated")
            return {
                "object": {"name": params["object_name"], "shape": {"solids": 1}},
                "part_path": params["part_path"],
                "part_ref": params["part_ref"],
                "verified": ["object.exists", "object.placement"],
            }
        return {"object_count": 0}


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    (root / "fasteners").mkdir(parents=True)
    (root / "fasteners" / "iso4014-m8x40.step").write_bytes(b"ISO-10303-21;")
    return root


@pytest.fixture
def configured(library: Path, monkeypatch) -> Path:
    monkeypatch.setenv(ENV_LIBRARY_ROOTS, str(library))
    return library


def _document(bridge: FreecadBridge, tmp_path: Path) -> Path:
    document = tmp_path / "assembly.FCStd"
    document.write_bytes(b"original-fcstd")
    return document


def test_listing_needs_no_freecad_host(configured: Path, tmp_path: Path):
    """The library is readable before a host exists, so it stays browsable."""
    bridge = FakeFreecad(tmp_path)
    bridge.executable = None

    result = bridge.list_parts()

    assert [part["path"] for part in result["parts"]] == ["fasteners/iso4014-m8x40.step"]
    assert bridge.calls == []


def test_unconfigured_library_is_reported_not_raised_by_capabilities(tmp_path: Path):
    bridge = FakeFreecad(tmp_path)

    library = bridge.capabilities()["parts_library"]

    assert library["configured"] is False
    assert library["offline_only"] is True
    assert library["remote_sources"] is False
    assert library["error_code"] == "parts_library_unavailable"
    assert library["env_var"] == ENV_LIBRARY_ROOTS


def test_capabilities_reports_the_configured_library(configured: Path, tmp_path: Path):
    bridge = FakeFreecad(tmp_path)

    library = bridge.capabilities()["parts_library"]

    assert library["configured"] is True
    assert library["roots"] == [str(configured.resolve())]
    assert library["max_limit"] == 500
    assert "insert_part" in bridge.capabilities()["methods"]
    assert "list_parts" in bridge.capabilities()["methods"]


def test_insert_sends_the_resolved_absolute_path_to_the_host(configured: Path, tmp_path: Path):
    bridge = FakeFreecad(tmp_path)
    document = _document(bridge, tmp_path)

    result = bridge.insert_part(
        str(document),
        "fasteners/iso4014-m8x40.step",
        "BoltM8x40",
        translation=[10, 20, 30],
        rotation_axis=[0, 0, 1],
        rotation_degrees=90,
    )

    methods = [call[0] for call in bridge.calls]
    assert methods == ["model.insert_part", "document.inspect"]
    _method, params, _timeout = bridge.calls[0]
    # The host receives a proven absolute path plus the ref it came from.
    assert params["part_path"] == str(configured / "fasteners" / "iso4014-m8x40.step")
    assert params["part_ref"] == "fasteners/iso4014-m8x40.step"
    assert params["part_root"] == str(configured.resolve())
    assert params["object_name"] == "BoltM8x40"
    assert params["translation"] == [10, 20, 30]
    assert params["rotation_degrees"] == 90
    # The mutation is proven by a read-back of the durable document; the
    # skill wrapper is what renames this list to verified_checks.
    assert result["verified"] == ["object.exists", "object.placement"]
    assert result["document_path"] == str(document)


@pytest.mark.parametrize(
    "part_ref",
    [
        "../outside.step",
        "fasteners/../../outside.step",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "https://example.com/part.step",
        "fasteners/readme.txt",
        "fasteners/absent.step",
    ],
)
def test_a_refused_reference_never_reaches_the_host(
    configured: Path, tmp_path: Path, part_ref: str
):
    """The whole point: no FreeCAD process, no staging copy, no change."""
    bridge = FakeFreecad(tmp_path)
    document = _document(bridge, tmp_path)
    before = document.read_bytes()

    with pytest.raises(PartLibraryError) as excinfo:
        bridge.insert_part(str(document), part_ref, "Intruder")

    assert excinfo.value.code in (
        "invalid_part_ref",
        "remote_library_unsupported",
        "unsupported_part_format",
        "part_not_found",
    ), part_ref
    assert bridge.calls == [], "%s must be refused before a host is started" % part_ref
    assert document.read_bytes() == before
    assert not list(tmp_path.glob(".*")), "a refusal must not leave a staging copy behind"


def test_an_unconfigured_library_refuses_the_insert_before_the_host(
    configured: Path, tmp_path, monkeypatch
):
    bridge = FakeFreecad(tmp_path)
    document = _document(bridge, tmp_path)
    monkeypatch.delenv(ENV_LIBRARY_ROOTS)

    with pytest.raises(PartLibraryError) as excinfo:
        bridge.insert_part(str(document), "fasteners/iso4014-m8x40.step", "Bolt")

    assert excinfo.value.code == "parts_library_unavailable"
    assert bridge.calls == []


def test_object_names_stay_bounded_on_insert(configured: Path, tmp_path: Path):
    bridge = FakeFreecad(tmp_path)
    document = _document(bridge, tmp_path)

    with pytest.raises(Exception, match="Object names"):
        bridge.insert_part(str(document), "fasteners/iso4014-m8x40.step", "9bolt")

    assert bridge.calls == []


def test_library_roots_are_not_required_to_sit_inside_allowed_roots(
    configured: Path, tmp_path: Path
):
    """A shared read-only library normally lives outside the project sandbox."""
    bridge = FakeFreecad(tmp_path / "sandbox")
    (tmp_path / "sandbox").mkdir()

    assert bridge.parts_library_roots() == [configured.resolve()]
