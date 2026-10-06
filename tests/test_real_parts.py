"""Real-host standard-part insert, on both pinned FreeCAD release lines.

The library is built offline out of the adapter's own exchange path: a
primitive is exported to STEP, and that STEP is what gets listed and inserted.
No fixture binaries and no download are involved, so the run exercises the same
containment resolution and the same read-back contract a real library would.

What is pinned here is not a magic number but an equivalence: the inserted
object's volume and bounding box must match the library file's, and the
document must pass ``validate_document`` afterwards.
"""

import os
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import FreecadBridge
from dcc_mcp_freecad.parts_library import ENV_LIBRARY_ROOTS, PartLibraryError

pytestmark = pytest.mark.freecad


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


@pytest.fixture(scope="module")
def real_executable():
    executable = _real_freecad()
    assert executable and Path(executable).is_file(), "Parts tests require real FreeCADCmd"
    assert os.environ.get("FREECAD_REAL_VERSION") in ("1.0.2", "1.1.4")
    return executable


@pytest.fixture(scope="module")
def parts_library(real_executable, tmp_path_factory):
    """Export a primitive to STEP once and serve that directory as a library.

    Module-scoped, so it sets the environment directly instead of through the
    function-scoped ``monkeypatch`` fixture.
    """
    root = tmp_path_factory.mktemp("parts-fixture")
    library = root / "library"
    (library / "fasteners").mkdir(parents=True)
    source = root / "library-source.FCStd"
    bridge = FreecadBridge(real_executable, allowed_roots=[root])
    bridge.create_document(str(source))
    bridge.add_primitive(
        str(source), "cylinder", "BoltShank", dimensions={"radius": 4, "height": 40}
    )
    part = library / "fasteners" / "iso4014-m8x40.step"
    bridge.export_geometry(str(source), ["BoltShank"], str(part))
    assert part.is_file() and part.stat().st_size > 0

    inspected = bridge.inspect_document(str(source))
    reference = next(obj for obj in inspected["objects"] if obj["name"] == "BoltShank")["shape"]
    previous = os.environ.get(ENV_LIBRARY_ROOTS)
    os.environ[ENV_LIBRARY_ROOTS] = str(library)
    try:
        yield {
            "executable": real_executable,
            "root": root,
            "library": library,
            "part_ref": "fasteners/iso4014-m8x40.step",
            "reference": reference,
        }
    finally:
        if previous is None:
            os.environ.pop(ENV_LIBRARY_ROOTS, None)
        else:
            os.environ[ENV_LIBRARY_ROOTS] = previous


@pytest.fixture
def assembly(parts_library, tmp_path):
    bridge = FreecadBridge(parts_library["executable"], allowed_roots=[tmp_path])
    document = tmp_path / "assembly.FCStd"
    bridge.create_document(str(document))
    return bridge, document


def test_real_standard_part_insert_passes_validation_and_matches_the_library(
    parts_library, assembly
):
    bridge, document_path = assembly
    reference = parts_library["reference"]

    listing = bridge.list_parts()
    assert [part["path"] for part in listing["parts"]] == [parts_library["part_ref"]]
    assert listing["parts"][0]["category"] == "fasteners"
    assert listing["parts"][0]["format"] == "step"

    inserted = bridge.insert_part(
        str(document_path), parts_library["part_ref"], "BoltM8x40", translation=[10, 20, 30]
    )

    shape = inserted["object"]["shape"]
    assert shape["valid"] is True
    assert shape["solids"] == 1
    assert shape["faces"] > 0 and shape["edges"] > 0 and shape["vertices"] > 0
    assert shape["volume"] == pytest.approx(reference["volume"], rel=1e-6)
    # The insert is the library file's geometry, moved where it was asked for.
    reference_box = reference["bounding_box"]
    expected_center = [
        center + shift for center, shift in zip(reference_box["center"], (10, 20, 30))
    ]
    assert shape["bounding_box"]["size"] == pytest.approx(reference_box["size"], rel=1e-6)
    assert shape["bounding_box"]["center"] == pytest.approx(expected_center, rel=1e-6, abs=1e-9)
    assert inserted["object"]["placement"]["translation"] == pytest.approx([10.0, 20.0, 30.0])
    assert inserted["verified"], "the insert must be proven by a read-back"

    validation = bridge.validate_document(str(document_path))
    assert validation["valid"] is True
    assert validation["invalid_objects"] == []
    assert validation["empty_shape_objects"] == []


def test_real_rotation_applies_to_the_inserted_part(parts_library, assembly):
    """A +90 degree turn about X maps (x, y, z) to (x, -z, y)."""
    bridge, document_path = assembly
    size = parts_library["reference"]["bounding_box"]["size"]
    center = parts_library["reference"]["bounding_box"]["center"]

    inserted = bridge.insert_part(
        str(document_path),
        parts_library["part_ref"],
        "BoltRotated",
        translation=[5, 0, 0],
        rotation_axis=[1, 0, 0],
        rotation_degrees=90,
    )

    box = inserted["object"]["shape"]["bounding_box"]
    assert box["size"] == pytest.approx([size[0], size[2], size[1]], rel=1e-6)
    assert box["center"] == pytest.approx(
        [5 + center[0], -center[2], center[1]], rel=1e-6, abs=1e-9
    )
    assert bridge.validate_document(str(document_path))["valid"] is True


HOSTILE_REFERENCES = (
    "../library-source.FCStd",
    "fasteners/../../library-source.FCStd",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
    "https://example.com/part.step",
    "fasteners/absent.step",
)


def test_real_hostile_references_are_refused_before_the_host_runs(parts_library, assembly):
    """``library-source.FCStd`` exists next to the library and stays unreachable.

    One document for every reference, so this stays one host round-trip per
    refusal instead of one per case: what is under test is the refusal, not the
    document, and a real host start is the expensive part.
    """
    bridge, document_path = assembly
    before = document_path.read_bytes()

    for part_ref in HOSTILE_REFERENCES:
        with pytest.raises(PartLibraryError) as excinfo:
            bridge.insert_part(str(document_path), part_ref, "Intruder")
        assert excinfo.value.code in (
            "invalid_part_ref",
            "remote_library_unsupported",
            "part_not_found",
        ), part_ref
        assert document_path.read_bytes() == before, part_ref

    validation = bridge.validate_document(str(document_path))
    assert validation["object_count"] == 0
    assert validation["valid"] is True
