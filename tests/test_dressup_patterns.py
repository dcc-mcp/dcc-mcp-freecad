"""Real-hardware coverage for dress-up, patterns and mirroring.

Every test here runs against a real FreeCADCmd on both supported release lines
(1.0.2 and 1.1.4 in CI) and asserts numbers, not just success. The thresholds
are written literally on purpose: a tolerance computed from the thing it checks
proves nothing, and a tolerance nobody wrote down is a tolerance nobody chose.

The geometry is deliberately simple -- a 40x40x20 box -- because the point of
these assertions is the arithmetic around the operation, not the shape.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge

# The test solid.
LENGTH = 40.0
WIDTH = 40.0
HEIGHT = 20.0
BOX_VOLUME = LENGTH * WIDTH * HEIGHT

# A pattern's volume is the source volume times the instance count. The
# deviation is floating point noise from summing transformed copies; one
# dropped instance would differ by a whole instance volume, sixteen orders of
# magnitude more than this.
PATTERN_VOLUME_REL_TOLERANCE = 1e-6

# A mirror is an isometry, so its volume must be bit-comparable to the source.
MIRROR_VOLUME_REL_TOLERANCE = 1e-9


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


def _box_document(bridge: FreecadBridge, tmp_path: Path, name: str = "part.FCStd"):
    document = tmp_path / name
    bridge.create_document(str(document))
    bridge.add_primitive(
        str(document),
        "box",
        "Body",
        dimensions={"length": LENGTH, "width": WIDTH, "height": HEIGHT},
    )
    return document


class _Tools:
    """The five tools bound to one document, so call sites stay readable."""

    def __init__(self, bridge: FreecadBridge, document: Path):
        self._bridge = bridge
        self._doc = str(document)

    def fillet(self, refs, radius, name):
        return self._bridge.fillet_edges(self._doc, "Body", refs, radius, name)

    def fillet_on(self, object_name, refs, radius, name):
        return self._bridge.fillet_edges(self._doc, object_name, refs, radius, name)

    def chamfer(self, refs, name, **extra):
        return self._bridge.chamfer_edges(self._doc, "Body", refs, name, **extra)

    def linear(self, count, name, spacing=60.0, direction=(1, 0, 0)):
        return self._bridge.linear_pattern(self._doc, "Body", list(direction), spacing, count, name)

    def polar(self, count, name, axis=(0, 0, 1), **extra):
        return self._bridge.polar_pattern(self._doc, "Body", list(axis), count, name, **extra)

    def mirror(self, plane, name, **extra):
        return self._bridge.mirror_feature(self._doc, "Body", plane, name, **extra)


def _expect_refusal(code, call):
    """Assert the call is refused with this code, and that the code is visible."""
    with pytest.raises(BridgeError) as excinfo:
        call()
    assert excinfo.value.code == code
    assert str(excinfo.value).startswith(code), "the code must survive in the message"


pytestmark = [
    pytest.mark.freecad,
    pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set"),
]


def test_fillet_removes_material_bounded_by_the_rounded_edges(tmp_path: Path):
    """A fillet on convex box edges removes material, and only along the edges.

    Rounding a straight convex edge of length ``L`` at radius ``r`` removes
    ``L * r**2 * (1 - pi/4)``: the square corner less the quarter cylinder that
    replaces it. Asserting that bound rather than a remembered number is what
    makes this a check instead of a recording -- and the direction is asserted
    because every edge of a box is convex, which is known here in a way the
    driver cannot know in general.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)

    radius = 2.0
    result = bridge.fillet_edges(str(document), "Body", [1, 2], radius, "Filleted")

    delta = result["volume_delta"]
    # Both requested edges are at most the longest box edge.
    bound = 2 * LENGTH * radius**2 * (1 - math.pi / 4)
    assert result["affected_edges"] == 2
    assert result["volume_delta_direction"] == "decreased"
    assert delta < 0, "a fillet on a convex edge removes material"
    assert abs(delta) <= bound + 1e-6, "the fillet removed more than the edges explain"
    assert abs((result["volume_before"] + delta) - result["volume_after"]) < 1e-9


def test_chamfer_removes_material_bounded_by_the_bevelled_edges(tmp_path: Path):
    """A symmetric chamfer of distance ``d`` removes ``L * d**2 / 2`` per edge."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)

    distance = 3.0
    result = bridge.chamfer_edges(str(document), "Body", [1, 2], "Chamfered", distance=distance)

    delta = result["volume_delta"]
    bound = 2 * LENGTH * distance**2 / 2
    assert result["affected_edges"] == 2
    assert result["volume_delta_direction"] == "decreased"
    assert delta < 0, "a chamfer on a convex edge removes material"
    assert abs(delta) <= bound + 1e-6, "the chamfer removed more than the edges explain"


def test_chamfer_accepts_asymmetric_distances_and_stores_both(tmp_path: Path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)

    result = bridge.chamfer_edges(str(document), "Body", [3], "Bevel", distance1=1.0, distance2=4.0)

    assert result["affected_edges"] == 1
    assert result["volume_delta"] != 0
    assert "result.edges[3]" in result["verified"]


def test_edge_ref_refusals_are_typed_and_leave_the_document_unchanged(tmp_path: Path):
    """A bad edge reference is refused outright, never applied to the good ones.

    The failure mode this closes is a partially applied edit reported as a
    success: the caller asked for three edges, two were valid, and the answer
    must not be "done".
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)
    before = document.read_bytes()
    tools = _Tools(bridge, document)

    for code, call in (
        ("E_EDGE_REF_OUT_OF_RANGE", lambda: tools.fillet([999], 1.0, "A")),
        ("E_EDGE_REF_OUT_OF_RANGE", lambda: tools.fillet([0], 1.0, "B")),
        ("E_EDGE_REF_DUPLICATE", lambda: tools.fillet([1, 1], 1.0, "C")),
        ("E_EDGE_REF_LIMIT", lambda: tools.fillet(list(range(1, 202)), 1.0, "D")),
        ("E_OBJECT_NOT_FOUND", lambda: tools.fillet_on("Nope", [1], 1.0, "E")),
        ("E_RESULT_EXISTS", lambda: tools.fillet([1], 1.0, "Body")),
    ):
        _expect_refusal(code, call)

    assert document.read_bytes() == before, "a refused call must not touch the source"


def test_infeasible_radius_is_refused_with_a_suggested_limit(tmp_path: Path):
    """A radius the faces cannot absorb is refused, not returned as a solid.

    The box's shortest dimension is 20mm, so a radius of half a metre cannot
    possibly fit. The refusal has to name a limit the caller can retry with;
    "the kernel said no" is not actionable.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)
    before = document.read_bytes()
    tools = _Tools(bridge, document)

    with pytest.raises(BridgeError) as excinfo:
        tools.fillet([1], 500.0, "Impossible")

    assert excinfo.value.code == "E_RADIUS_NOT_FEASIBLE"
    assert "absorbs below" in str(excinfo.value), "the refusal must suggest a usable limit"

    # A radius that fits must still be accepted, which pins the bound from the
    # other side: a check that rejected everything would also pass above.
    ok = tools.fillet([1], 5.0, "Feasible")
    assert ok["volume_delta"] != 0
    assert document.read_bytes() != before


def test_linear_pattern_volume_is_the_source_times_the_instance_count(tmp_path: Path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)

    count = 4
    result = bridge.linear_pattern(str(document), "Body", [1, 0, 0], 60.0, count, "FinArray")

    expected = BOX_VOLUME * count
    assert result["instance_count"] == count
    assert result["overlap_detected"] is False
    assert result["min_instance_gap"] > 0
    assert abs(result["volume"] - expected) <= PATTERN_VOLUME_REL_TOLERANCE * expected
    assert abs(result["volume_deviation"]) <= PATTERN_VOLUME_REL_TOLERANCE * expected
    assert result["object"]["shape"]["solids"] == count


def test_linear_pattern_reports_instances_that_overlap(tmp_path: Path):
    """A spacing smaller than the part stacks instances, and the tool says so."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)
    tools = _Tools(bridge, document)

    clear = tools.linear(3, "Clear", spacing=60.0)
    crowded = tools.linear(3, "Crowded", spacing=20.0)

    assert clear["overlap_detected"] is False
    assert crowded["overlap_detected"] is True
    assert crowded["min_instance_gap"] < 0
    # The volume check still passes: coincident material still sums.
    assert abs(crowded["volume_deviation"]) <= (
        PATTERN_VOLUME_REL_TOLERANCE * crowded["expected_volume"]
    )


def test_polar_pattern_accepts_either_an_angular_step_or_a_total_sweep(tmp_path: Path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)
    tools = _Tools(bridge, document)
    center = {"center": [200, 0, 0]}

    by_step = tools.polar(6, "ByStep", angle_step_degrees=60.0, **center)
    by_total = tools.polar(6, "ByTotal", total_angle_degrees=300.0, **center)

    assert by_step["detail"]["total_angle_degrees"] == pytest.approx(300.0)
    assert by_total["detail"]["angle_step_degrees"] == pytest.approx(60.0)
    expected = BOX_VOLUME * 6
    for result in (by_step, by_total):
        assert result["instance_count"] == 6
        assert abs(result["volume"] - expected) <= PATTERN_VOLUME_REL_TOLERANCE * expected

    # Two ways of asking for the same thing must produce the same placement.
    assert by_step["object"]["shape"]["bounding_box"] == pytest.approx(
        by_total["object"]["shape"]["bounding_box"], rel=1e-9, abs=1e-9
    )

    _expect_refusal(
        "E_ANGLE_CONFLICT",
        lambda: tools.polar(4, "X", angle_step_degrees=10.0, total_angle_degrees=30.0),
    )
    _expect_refusal("E_ANGLE_REQUIRED", lambda: tools.polar(4, "Y"))
    _expect_refusal(
        "E_ZERO_VECTOR",
        lambda: tools.polar(4, "Z", axis=(0, 0, 0), angle_step_degrees=90.0),
    )
    _expect_refusal("E_INSTANCE_LIMIT", lambda: tools.polar(1001, "W", angle_step_degrees=1.0))


def test_pattern_refusals_are_typed_and_leave_the_document_unchanged(tmp_path: Path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)
    before = document.read_bytes()
    tools = _Tools(bridge, document)

    for code, call in (
        ("E_INSTANCE_LIMIT", lambda: tools.linear(5000, "A")),
        ("E_ZERO_VECTOR", lambda: tools.linear(3, "B", direction=(0, 0, 0))),
        ("E_PATTERN_SPACING", lambda: tools.linear(3, "C", spacing=0.0)),
        ("E_RESULT_EXISTS", lambda: tools.linear(3, "Body")),
    ):
        _expect_refusal(code, call)

    assert document.read_bytes() == before


def test_mirror_preserves_volume_and_topology(tmp_path: Path):
    """A mirror is an isometry, which makes its result knowable in advance.

    Volume and the solid, face, edge and vertex counts cannot move. Asserting
    all five is what separates "the host mirrored the shape" from "the host
    built something the same size".
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)
    tools = _Tools(bridge, document)

    source = bridge.inspect_document(str(document))
    shape = next(obj for obj in source["objects"] if obj["name"] == "Body")["shape"]

    for plane in ("yz", "xz", "xy"):
        result = tools.mirror(plane, "Mirror%s" % plane)
        mirrored = result["object"]["shape"]
        limit = MIRROR_VOLUME_REL_TOLERANCE * abs(shape["volume"])
        assert abs(result["volume"] - shape["volume"]) <= limit
        for attribute in ("solids", "faces", "edges", "vertices"):
            assert mirrored[attribute] == shape[attribute], "%s moved the %s" % (
                plane,
                attribute,
            )
        assert "result.volume" in result["verified"]

    _expect_refusal("E_PLANE_INVALID", lambda: tools.mirror("nope", "BadPlane"))


def test_dress_up_results_survive_a_durable_reopen(tmp_path: Path):
    """The change is in the saved file, not only in the host that made it.

    A fillet that exists in memory and vanishes on reopen is the exact shape of
    "reported success, model unchanged", so the document is read back through a
    fresh inspection after each write.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = _box_document(bridge, tmp_path)

    fillet = bridge.fillet_edges(str(document), "Body", [1, 2], 2.0, "Filleted")
    pattern = bridge.linear_pattern(str(document), "Body", [1, 0, 0], 60.0, 3, "FinArray")

    inspected = bridge.inspect_document(str(document))
    by_name = dict((obj["name"], obj) for obj in inspected["objects"])

    assert by_name["Filleted"]["type_id"] == "Part::Fillet"
    assert by_name["Filleted"]["shape"]["volume"] == pytest.approx(fillet["volume_after"], rel=1e-9)
    assert by_name["FinArray"]["shape"]["solids"] == 3
    assert by_name["FinArray"]["shape"]["volume"] == pytest.approx(pattern["volume"], rel=1e-9)
    # The dress-up result is wired to the source it was built from.
    assert "Body" in by_name["Filleted"]["outgoing_links"]
    assert bridge.validate_document(str(document))["valid"] is True
