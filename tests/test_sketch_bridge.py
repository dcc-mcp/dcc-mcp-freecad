"""Sketch tools across the process boundary.

The bridge is what turns a refusal inside FreeCAD into something an agent can
branch on: a sketch that must not be used as a feature profile has to arrive as
a machine-readable error code, not as a sentence that differs between host
versions. These tests pin that, plus the two properties the sketch tools share
with every other mutating tool in the adapter: a failed edit leaves the original
bytes untouched, and a read-only inspection stages nothing.

The real-host lane at the bottom is the dual-version evidence: the same sketch
call sequence must produce the same topology on FreeCAD 1.0.2 and 1.1.4.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
from test_bridge import FakeFreecad

from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge, SketchStateError
from dcc_mcp_freecad.sketch_rules import CONSTRAINT_TYPES, GEOMETRY_TYPES, PLANES


class RejectingSketch(FakeFreecad):
    """A host that refuses a sketch the way the driver refuses it."""

    def __init__(self, root: Path, error_code: str):
        super().__init__(root)
        self.error_code = error_code
        self.reject_method = "sketch.info"

    def _invoke(self, method, params, timeout_secs=120):
        if method == self.reject_method:
            raise SketchStateError("sketch refused by the host", self.error_code, {"dof": 2})
        return super()._invoke(method, params, timeout_secs)


def test_capabilities_declare_the_sketch_surface():
    bridge = FakeFreecad(Path.cwd())

    capabilities = bridge.capabilities()

    for method in (
        "create_sketch",
        "add_sketch_geometry",
        "add_sketch_constraint",
        "get_sketch_info",
    ):
        assert method in capabilities["methods"], method
    assert capabilities["sketch_planes"] == list(PLANES)
    assert capabilities["sketch_geometry_types"] == list(GEOMETRY_TYPES)
    assert capabilities["sketch_constraint_types"] == list(CONSTRAINT_TYPES)
    assert capabilities["sketch_underconstrained_rejected"] is True
    assert capabilities["arbitrary_python"] is False


def test_a_sketch_refusal_reaches_the_caller_as_an_error_code(tmp_path: Path):
    """The error code is the contract; the message is only for humans."""
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = RejectingSketch(tmp_path, "E_SKETCH_UNDERCONSTRAINED")

    with pytest.raises(SketchStateError) as excinfo:
        bridge.get_sketch_info(str(document), "Sketch", require_fully_constrained=True)

    error = excinfo.value
    assert isinstance(error, BridgeError), "existing handlers must still catch it"
    assert error.code == "E_SKETCH_UNDERCONSTRAINED"
    assert error.state == {"dof": 2}
    assert document.read_bytes() == b"known-good"


def test_a_failed_sketch_mutation_preserves_the_original_bytes(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)
    bridge.fail_method = "sketch.add_geometry"

    with pytest.raises(BridgeError, match="simulated"):
        bridge.add_sketch_geometry(
            str(document), "Sketch", [{"type": "circle", "cx": 0, "cy": 0, "radius": 4}]
        )

    assert document.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".model.*.FCStd"))


def test_a_successful_sketch_mutation_replaces_the_document(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    result = bridge.create_sketch(str(document), "Sketch", plane="xz")

    assert document.read_bytes() == b"known-good-mutated"
    assert result["document_path"] == str(document)
    assert len(result["document_sha256"]) == 64


def test_sketch_names_are_validated_before_the_host_is_called(tmp_path: Path):
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    with pytest.raises(BridgeError, match="Object names"):
        bridge.create_sketch(str(document), "bad name")
    with pytest.raises(BridgeError, match="plane must be one of"):
        bridge.create_sketch(str(document), "Sketch", plane="zx")


def test_sketch_info_stages_nothing_and_never_rewrites_the_document(tmp_path: Path):
    """An inspection is not a mutation: the document must come back identical."""
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    result = bridge.get_sketch_info(str(document), "Sketch")

    assert result["object_count"] == 0
    assert document.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".model.*")), "a read-only call must not stage a copy"


def test_every_declared_geometry_and_constraint_type_is_reachable():
    """A type the tool advertises has to exist in the rules, not only in yaml."""
    bridge = FakeFreecad(Path.cwd())
    capabilities = bridge.capabilities()

    assert set(capabilities["sketch_geometry_types"]) == set(GEOMETRY_TYPES)
    assert set(capabilities["sketch_constraint_types"]) == set(CONSTRAINT_TYPES)
    assert "coincident" in capabilities["sketch_constraint_types"]
    for name in ("radius", "distance", "angle"):
        assert name in capabilities["sketch_constraint_types"], name


# ---------------------------------------------------------------------------
# Real host: the same call sequence, FreeCAD 1.0.2 and 1.1.4
# ---------------------------------------------------------------------------


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


def _rectangle_constraints():
    """Corner coincidences plus dimensions: enough to pin the rectangle."""
    return [
        {"type": "horizontal", "first": {"element": 0}},
        {"type": "horizontal", "first": {"element": 2}},
        {"type": "vertical", "first": {"element": 1}},
        {"type": "vertical", "first": {"element": 3}},
        {
            "type": "coincident",
            "first": {"element": 0, "vertex": 2},
            "second": {"element": 1, "vertex": 1},
        },
        {
            "type": "coincident",
            "first": {"element": 1, "vertex": 2},
            "second": {"element": 2, "vertex": 1},
        },
        {
            "type": "coincident",
            "first": {"element": 2, "vertex": 2},
            "second": {"element": 3, "vertex": 1},
        },
        {
            "type": "coincident",
            "first": {"element": 3, "vertex": 2},
            "second": {"element": 0, "vertex": 1},
        },
        {"type": "length", "first": {"element": 0}, "value": 40},
        {"type": "length", "first": {"element": 1}, "value": 10},
        {
            "type": "distance_x",
            "first": {"element": -1, "vertex": 1},
            "second": {"element": 0, "vertex": 1},
            "value": 0,
        },
        {
            "type": "distance_y",
            "first": {"element": -1, "vertex": 1},
            "second": {"element": 0, "vertex": 1},
            "value": 0,
        },
    ]


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_builds_a_fully_constrained_sketch(tmp_path: Path):
    """The dual-version topology gate for the sketch main path.

    The counts are the point of the test: the same typed call sequence has to
    arrive at the same vertex/edge/face counts on every supported host, because
    a sketch that solves differently per version is not reproducible.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "sketch.FCStd"
    bridge.create_document(str(document))

    created = bridge.create_sketch(str(document), "ProfileSketch", plane="xy")
    assert created["body"]["created"] is True
    assert created["attachment"]["plane"] == "xy"
    assert created["sketch"]["type_id"] == "Sketcher::SketchObject"

    geometry = bridge.add_sketch_geometry(
        str(document),
        "ProfileSketch",
        [{"type": "rectangle", "x": 0, "y": 0, "width": 40, "height": 10}],
    )
    assert [item["index"] for item in geometry["elements"]] == [0, 1, 2, 3]

    constrained = bridge.add_sketch_constraint(
        str(document), "ProfileSketch", _rectangle_constraints()
    )
    assert constrained["sketch"]["constraint_count"] == len(_rectangle_constraints())

    info = bridge.get_sketch_info(str(document), "ProfileSketch")
    assert info["dof"] == 0, info
    assert info["dof_source"], "the host must say where the number came from"
    assert info["feature_state"]["fully_constrained"] is True
    assert info["feature_state"]["feature_ready"] is True
    assert info["external_geometry_count"] == 0
    assert info["topology"]["vertices"] == 4, info["topology"]
    assert info["topology"]["edges"] == 4, info["topology"]
    assert info["topology"]["faces"] == 0, info["topology"]
    assert len(info["geometry"]) == 4
    assert {item["kind"] for item in info["geometry"]} == {"line"}


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_refuses_an_underconstrained_sketch(tmp_path: Path):
    """An under-constrained sketch must fail loudly, not solve quietly.

    This is the acceptance criterion the whole sketch surface exists for: with
    no script escape hatch there is no way to repair the sketch afterwards, so
    a profile that is not fully constrained has to be refused here rather than
    discovered as a different shape on the next host.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "loose.FCStd"
    bridge.create_document(str(document))
    bridge.create_sketch(str(document), "LooseSketch", plane="xy")
    bridge.add_sketch_geometry(
        str(document),
        "LooseSketch",
        [{"type": "rectangle", "x": 0, "y": 0, "width": 40, "height": 10}],
    )
    # Corners closed and the sides axis-aligned, but nothing fixes the size or
    # the position: the rectangle can still move and scale.
    bridge.add_sketch_constraint(
        str(document),
        "LooseSketch",
        [
            {"type": "horizontal", "first": {"element": 0}},
            {"type": "horizontal", "first": {"element": 2}},
            {"type": "vertical", "first": {"element": 1}},
            {"type": "vertical", "first": {"element": 3}},
            {
                "type": "coincident",
                "first": {"element": 0, "vertex": 2},
                "second": {"element": 1, "vertex": 1},
            },
            {
                "type": "coincident",
                "first": {"element": 1, "vertex": 2},
                "second": {"element": 2, "vertex": 1},
            },
            {
                "type": "coincident",
                "first": {"element": 2, "vertex": 2},
                "second": {"element": 3, "vertex": 1},
            },
            {
                "type": "coincident",
                "first": {"element": 3, "vertex": 2},
                "second": {"element": 0, "vertex": 1},
            },
        ],
    )

    reported = bridge.get_sketch_info(str(document), "LooseSketch")
    assert reported["dof"] > 0, reported
    assert reported["feature_state"]["fully_constrained"] is False
    assert reported["feature_state"]["feature_ready"] is False
    assert reported["feature_state"]["blocking_error_code"] == "E_SKETCH_UNDERCONSTRAINED"

    before_refusal = document.read_bytes()
    with pytest.raises(SketchStateError) as excinfo:
        bridge.get_sketch_info(str(document), "LooseSketch", require_fully_constrained=True)
    assert excinfo.value.code == "E_SKETCH_UNDERCONSTRAINED"
    assert document.read_bytes() == before_refusal

    with pytest.raises(SketchStateError) as excinfo:
        bridge.add_sketch_constraint(
            str(document),
            "LooseSketch",
            [{"type": "horizontal", "first": {"element": 99}}],
        )
    assert excinfo.value.code == "E_SKETCH_ELEMENT_NOT_FOUND"
    assert document.read_bytes() == before_refusal, "a refused constraint must not half-apply"


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_constrains_a_circle_by_radius_and_position(tmp_path: Path):
    """The dimensional path: a radius is stored and read back as a radius."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "bore.FCStd"
    bridge.create_document(str(document))
    bridge.create_sketch(str(document), "BoreSketch", plane="xy", body="BoreBody")

    bridge.add_sketch_geometry(
        str(document), "BoreSketch", [{"type": "circle", "cx": 20, "cy": 5, "radius": 4}]
    )
    constrained = bridge.add_sketch_constraint(
        str(document),
        "BoreSketch",
        [
            {"type": "radius", "first": {"element": 0}, "value": 4},
            {
                "type": "distance_x",
                "first": {"element": -1, "vertex": 1},
                "second": {"element": 0, "vertex": 3},
                "value": 20,
            },
            {
                "type": "distance_y",
                "first": {"element": -1, "vertex": 1},
                "second": {"element": 0, "vertex": 3},
                "value": 5,
            },
        ],
    )

    assert constrained["constraints"][0]["type"] == "Radius"
    assert constrained["constraints"][0]["value"] == 4.0
    assert constrained["feature_state"]["feature_ready"] is True

    info = bridge.get_sketch_info(str(document), "BoreSketch")
    assert info["dof"] == 0, info
    assert info["topology"]["edges"] == 1, info["topology"]
    assert info["topology"]["faces"] == 0, info["topology"]
    assert info["geometry"][0]["kind"] == "circle"
    assert info["geometry"][0]["key_points"][2] == 4.0


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
@pytest.mark.parametrize("start,end,mid_quadrant", [(0, 90, 1), (90, 0, 1), (180, 270, 3)])
def test_real_freecad_stores_the_arc_direction_it_was_given(
    tmp_path: Path, start, end, mid_quadrant
):
    """An arc must come back as the sweep that was requested, in both directions.

    ``sense`` is not "which way to sweep": on the periodic basis of a circle,
    SetTrim keeps the angles in order for a true Sense and reverses the curve for
    a false one, which is what turned a 90 -> 0 request into a 270 degree
    complement. Both endpoints, the centre and the radius match either way, so the
    midpoint is the value that tells them apart -- asserted once per direction, on
    every host in the matrix, because the answer must not vary between 1.0.2 and
    1.1.4.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / ("arc-%d-%d.FCStd" % (start, end))
    bridge.create_document(str(document))
    bridge.create_sketch(str(document), "ArcSketch", plane="xy", body="ArcBody")

    added = bridge.add_sketch_geometry(
        str(document),
        "ArcSketch",
        [
            {
                "type": "arc",
                "cx": 0,
                "cy": 0,
                "radius": 10,
                "start_angle_degrees": start,
                "end_angle_degrees": end,
            }
        ],
    )

    # start, midpoint, end, centre, radius
    points = added["elements"][0]["key_points"]
    mid_x, mid_y = points[2], points[3]

    expected_mid = 45.0 if mid_quadrant == 1 else 225.0
    assert math.isclose(mid_x, 10 * math.cos(math.radians(expected_mid)), abs_tol=1e-6), (
        "the arc swept the wrong way: midpoint %r for %d -> %d" % (points[2:4], start, end)
    )
    assert math.isclose(mid_y, 10 * math.sin(math.radians(expected_mid)), abs_tol=1e-6), (
        "the arc swept the wrong way: midpoint %r for %d -> %d" % (points[2:4], start, end)
    )

    info = bridge.get_sketch_info(str(document), "ArcSketch")
    assert info["geometry"][0]["kind"] == "arc"
    assert info["topology"]["edges"] == 1, info["topology"]
