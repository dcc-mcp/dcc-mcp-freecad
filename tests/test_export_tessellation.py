"""Real-hardware tessellation regressions for ``model.export_geometry``.

The contract suite in ``test_write_contract.py`` pins the export postconditions
against a stand-in host, so it can prove that *this adapter* refuses an empty
mesh. What it can never prove is that the real ``MeshPart.meshFromShape`` call
behaves: a host that tessellates a curved solid into nothing raises inside
FreeCAD, long before any postcondition of ours runs. Upstream reports #96 and
#121 are exactly that failure, and a fake host cannot reproduce it.

These tests therefore run against a real FreeCADCmd and read the artefact back
the way a consumer would: by parsing the STL, not by trusting the host's own
facet count. They cover the four classes the contract suite cannot reach --

* curved surfaces (sphere / torus / cone), where a tessellation misuse shows up
  as a plausible-looking file carrying no geometry;
* the deflection bounds, where a swallowed parameter is only visible as a
  facet count that did not move;
* byte stability, so an export is reproducible rather than merely present;
* a degenerate request, which must be refused instead of written as a shell.

Every export here is bounded in both directions. A curved solid that collapses
to a handful of triangles is as much a bug as one that explodes, so the lower
bound is set above the twelve facets a single planar box needs: anything at or
below that has not tessellated the curvature at all.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import BridgeError, BridgeTimeoutError, FreecadBridge

# A planar box needs 12 triangles. A genuinely tessellated curved surface needs
# far more, so this doubles as the "the curvature was actually meshed" bound.
_MIN_CURVED_FACETS = 12

# Runaway guard. A fine deflection on a small solid stays in the thousands; a
# misuse that ignores the requested deflection shows up in the millions.
_MAX_FACETS = 5_000_000


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


pytestmark = [
    pytest.mark.freecad,
    pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set"),
]


def _stl_facet_count(path: Path) -> int:
    """Count triangles by parsing the STL, independent of the host that wrote it.

    Reading the file back is the point: asking FreeCAD how many facets it wrote
    would let a host that reports one thing and writes another pass.
    """
    data = path.read_bytes()
    if len(data) >= 84:
        candidate = struct.unpack("<I", data[80:84])[0]
        if len(data) == 84 + 50 * candidate:
            return candidate
    if data.startswith(b"solid"):
        return data.count(b"facet normal")
    return 0


@pytest.fixture(scope="module")
def curved_document(tmp_path_factory):
    """One document holding the curved solids the export tests share.

    Building the document costs a subprocess per primitive, and every assertion
    below is about tessellation rather than creation, so the geometry is made
    once for the module and each test pays only for its own exports.
    """
    root = tmp_path_factory.mktemp("export-tessellation")
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[root])
    document = root / "curved.FCStd"
    bridge.create_document(str(document))
    bridge.add_primitive(str(document), "sphere", "CurvedSphere", dimensions={"radius": 5})
    bridge.add_primitive(
        str(document), "torus", "CurvedTorus", dimensions={"radius1": 20, "radius2": 4}
    )
    bridge.add_primitive(
        str(document),
        "cone",
        "CurvedCone",
        dimensions={"radius1": 3, "radius2": 10, "height": 24},
    )
    # A zero-radius apex is the degenerate case: a genuine point contact, and a
    # legal primitive because the schema allows radius1 of 0.
    bridge.add_primitive(
        str(document),
        "cone",
        "ApexCone",
        dimensions={"radius1": 0, "radius2": 10, "height": 24},
    )
    return bridge, document


def _export(
    bridge: FreecadBridge,
    document: Path,
    output: Path,
    object_name: str,
    linear_deflection: float = 0.1,
    angular_deflection_degrees: float = 15,
) -> dict:
    return bridge.export_geometry(
        str(document),
        [object_name],
        str(output),
        linear_deflection=linear_deflection,
        angular_deflection_degrees=angular_deflection_degrees,
    )


def _assert_bounded_mesh(output: Path, result: dict) -> int:
    """Assert the artefact is a real, bounded mesh and return its facet count."""
    assert result["bytes"] > 84, "an STL with only its 84-byte header carries no geometry"
    assert "artifact.mesh_non_empty" in result["verified"]
    facets = _stl_facet_count(output)
    assert facets > _MIN_CURVED_FACETS, (
        "%s tessellated into %d facets; a curved surface collapsed to a planar shell"
        % (output.name, facets)
    )
    assert facets <= _MAX_FACETS, "tessellation ran away with %d facets" % facets
    return facets


@pytest.mark.parametrize("object_name", ["CurvedSphere", "CurvedTorus", "CurvedCone"])
def test_curved_surfaces_tessellate_into_bounded_meshes(curved_document, tmp_path, object_name):
    """A curved solid must export real geometry, not a legal file with nothing in it.

    This is the regression for upstream #96 and #121: both report an export that
    fails or empties out on curved geometry, and both are invisible to a suite
    that only ever exports boxes and cylinders.
    """
    bridge, document = curved_document
    output = tmp_path / ("%s.stl" % object_name)

    result = _export(bridge, document, output, object_name)

    _assert_bounded_mesh(output, result)


def _export_or_refusal(bridge, document, output, **deflections):
    """Export at a deflection the host may legitimately refuse.

    Near the documented bounds a host is allowed to say no rather than return a
    mesh it cannot bound, so the pair returned here distinguishes a refusal from
    an accepted export instead of forcing one outcome. A timeout is not a
    refusal -- it is a host that never answered, and treating the two alike
    would let a hang near the bounds pass as a clean rejection.
    """
    try:
        return _export(bridge, document, output, "CurvedSphere", **deflections)
    except BridgeTimeoutError:
        raise
    except BridgeError:
        assert not output.exists(), "a refused export must not leave a file"
        return None


def test_linear_deflection_bounds_stay_bounded_and_move_the_mesh(curved_document, tmp_path):
    """The requested linear deflection must reach the tessellator.

    A deflection that is accepted and then ignored leaves the caller with a mesh
    they did not ask for, which is only observable in the facet count. The fine
    end is asserted hard -- a valid sphere at a fine deflection has no excuse for
    coming back empty -- while the upper bound is allowed to be refused, because
    a deflection wider than the solid itself is a legitimate thing to reject.
    """
    bridge, document = curved_document
    fine = tmp_path / "linear-fine.stl"
    default = tmp_path / "linear-default.stl"
    extreme = tmp_path / "linear-extreme.stl"

    fine_result = _export(bridge, document, fine, "CurvedSphere", linear_deflection=0.001)
    default_result = _export(bridge, document, default, "CurvedSphere", linear_deflection=0.1)
    extreme_result = _export_or_refusal(bridge, document, extreme, linear_deflection=99.9)

    fine_facets = _assert_bounded_mesh(fine, fine_result)
    default_facets = _assert_bounded_mesh(default, default_result)

    assert fine_facets > default_facets, (
        "a 0.001 deflection produced %d facets against %d at the default; "
        "the parameter did not reach the tessellator" % (fine_facets, default_facets)
    )
    if extreme_result is not None:
        extreme_facets = _assert_bounded_mesh(extreme, extreme_result)
        assert extreme_facets <= default_facets, (
            "a coarser deflection produced more facets than the default "
            "(%d vs %d)" % (extreme_facets, default_facets)
        )


def test_angular_deflection_bounds_stay_bounded_and_move_the_mesh(curved_document, tmp_path):
    """The requested angular deflection must reach the tessellator.

    The same property as the linear bound, swept over the documented 0-180 degree
    range, with the same split: the fine end must produce a real mesh, the upper
    bound may be refused but must never come back degenerate.
    """
    bridge, document = curved_document
    fine = tmp_path / "angular-fine.stl"
    default = tmp_path / "angular-default.stl"
    extreme = tmp_path / "angular-extreme.stl"

    fine_result = _export(bridge, document, fine, "CurvedSphere", angular_deflection_degrees=1.0)
    default_result = _export(
        bridge, document, default, "CurvedSphere", angular_deflection_degrees=15
    )
    extreme_result = _export_or_refusal(bridge, document, extreme, angular_deflection_degrees=179.9)

    fine_facets = _assert_bounded_mesh(fine, fine_result)
    default_facets = _assert_bounded_mesh(default, default_result)

    assert fine_facets > default_facets, (
        "a 1 degree deflection produced %d facets against %d at the default; "
        "the parameter did not reach the tessellator" % (fine_facets, default_facets)
    )
    if extreme_result is not None:
        extreme_facets = _assert_bounded_mesh(extreme, extreme_result)
        assert extreme_facets <= default_facets, (
            "a coarser deflection produced more facets than the default "
            "(%d vs %d)" % (extreme_facets, default_facets)
        )


def _stl_payload_sha256(path: Path) -> str:
    """Hash the triangles, skipping the 80-byte STL header.

    The header is FreeCAD's to fill, and whether it stamps a timestamp there is
    not something this adapter promises. What must be reproducible is the
    tessellation, so the hash covers the triangle data a consumer reads.
    """
    data = path.read_bytes()
    if len(data) >= 84 and len(data) == 84 + 50 * struct.unpack("<I", data[80:84])[0]:
        return hashlib.sha256(data[84:]).hexdigest()
    return hashlib.sha256(data).hexdigest()


def test_repeated_curved_export_is_byte_stable(curved_document, tmp_path):
    """The same request must produce the same artefact twice.

    An export that only "did not raise" is what the upstream reports complain
    about; a reproducible result is what makes the output trustworthy enough to
    diff in a pipeline. A host that re-tessellates differently between two
    identical calls is unusable for that, and no postcondition catches it.
    """
    bridge, document = curved_document
    first = tmp_path / "stable-first.stl"
    second = tmp_path / "stable-second.stl"

    first_result = _export(bridge, document, first, "CurvedTorus")
    second_result = _export(bridge, document, second, "CurvedTorus")

    first_facets = _assert_bounded_mesh(first, first_result)
    second_facets = _assert_bounded_mesh(second, second_result)

    assert first_facets == second_facets, (
        "two identical exports tessellated into %d and %d facets" % (first_facets, second_facets)
    )
    assert _stl_payload_sha256(first) == _stl_payload_sha256(second), (
        "two identical exports of the same solid differ "
        "(whole-file %s vs %s); tessellation is not reproducible"
        % (first_result["sha256"], second_result["sha256"])
    )


def test_an_unbounded_tessellation_is_refused_not_written_as_a_shell(curved_document, tmp_path):
    """A degenerate request must fail loudly, never as a legal empty file.

    Both deflections at their documented maximum remove every tessellation
    constraint at once, on a solid with a true point contact. Whatever the host
    does with that, the one outcome the contract forbids is a written file that
    carries no geometry.
    """
    bridge, document = curved_document
    output = tmp_path / "degenerate.stl"

    try:
        result = _export(
            bridge,
            document,
            output,
            "ApexCone",
            linear_deflection=99.9,
            angular_deflection_degrees=179.9,
        )
    except BridgeError:
        # Refused, and the bridge must not leave the fragment behind.
        assert not output.exists(), "a refused export must not leave a file"
        return

    # Accepted, so it has to be real geometry rather than an empty shell.
    assert result["bytes"] > 84
    facets = _stl_facet_count(output)
    assert facets > 0, "an accepted export wrote a file carrying no triangles"
    assert facets <= _MAX_FACETS
