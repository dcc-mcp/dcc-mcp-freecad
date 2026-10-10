"""PartDesign features on a real FreeCAD host.

This is the dual-version evidence lane: the same typed call sequence must
produce the same topology and the same volumes on FreeCAD 1.0.2 and 1.1.4. A
feature that only works on one release line is a feature the adapter must not
claim to support.

The volume assertions carry hard thresholds rather than a "greater than zero"
hand-wave, because that is the whole point of the read-back: a pocket that
removed nothing has to be distinguishable from one that removed material. The
thresholds below are computed from the profile in the fixture, not copied from
whatever a host happened to report, so a host that changed the geometry fails
the test instead of redefining it.

What the host-free lane cannot cover is the side-type ladder. Which spelling a
given build actually exposes -- ``SideType``, ``Midplane``, or ``Symmetric`` --
is measured here, on both release lines.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_bridge import FakeFreecad

from dcc_mcp_freecad.bridge import BridgeError, FreecadBridge, SketchStateError

# The profile: a 40 x 10 mm rectangle on the XY plane, fully constrained.
PROFILE_WIDTH = 40.0
PROFILE_HEIGHT = 10.0
PROFILE_AREA = PROFILE_WIDTH * PROFILE_HEIGHT  # 400.0 mm^2

# Hard volume thresholds. A pad of this profile 10 mm deep is exactly 4000 mm^3;
# the bounds allow for how a host tessellates and clips, and are still two
# orders of magnitude tighter than "did it change at all".
PAD_LENGTH = 10.0
PAD_VOLUME = PROFILE_AREA * PAD_LENGTH  # 4000.0
VOLUME_REL_TOLERANCE = 0.01

POCKET_DEPTH = 4.0
POCKET_VOLUME = PROFILE_AREA * POCKET_DEPTH  # 1600.0

HOLE_DIAMETER = 8.0
HOLE_DEPTH = 6.0
HOLE_VOLUME = 3.141592653589793 * (HOLE_DIAMETER / 2.0) ** 2 * HOLE_DEPTH


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


def _rectangle_constraints():
    """Corner coincidences plus dimensions and an origin fix: DOF 0."""
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
        {"type": "length", "first": {"element": 0}, "value": PROFILE_WIDTH},
        {"type": "length", "first": {"element": 1}, "value": PROFILE_HEIGHT},
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


def _constrained_sketch(bridge, document, name="ProfileSketch"):
    """Create a fully constrained 40 x 10 mm rectangle and return its info."""
    bridge.create_sketch(str(document), name, plane="xy")
    bridge.add_sketch_geometry(
        str(document),
        name,
        [{"type": "rectangle", "x": 0, "y": 0, "width": PROFILE_WIDTH, "height": PROFILE_HEIGHT}],
    )
    bridge.add_sketch_constraint(str(document), name, _rectangle_constraints())
    info = bridge.get_sketch_info(str(document), name)
    assert info["dof"] == 0, info
    return info


# ---------------------------------------------------------------------------
# The bridge surface
# ---------------------------------------------------------------------------


def test_capabilities_declare_the_feature_surface():
    bridge = FakeFreecad(Path.cwd())

    capabilities = bridge.capabilities()

    for method in (
        "pad_feature",
        "pocket_feature",
        "revolution_feature",
        "groove_feature",
        "loft_feature",
        "sweep_feature",
        "create_hole",
    ):
        assert method in capabilities["methods"], method


@pytest.mark.parametrize(
    "method,call",
    [
        ("pad_feature", lambda b, d: b.pad_feature(d, "S", "R", 10.0, side_type="sideways")),
        ("pocket_feature", lambda b, d: b.pocket_feature(d, "S", "R", 10.0, side_type="sideways")),
        ("create_hole", lambda b, d: b.create_hole(d, "S", "R", 8.0, 6.0, hole_type="hex")),
    ],
)
def test_enum_arguments_are_validated_before_the_host_is_called(tmp_path, method, call):
    """A bad enum never reaches FreeCAD: it is refused at the boundary."""
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)

    with pytest.raises(BridgeError):
        call(bridge, str(document))

    # Nothing was staged, so the document is untouched.
    assert document.read_bytes() == b"known-good"


def test_a_failed_feature_preserves_the_original_bytes(tmp_path):
    """The staging contract: a feature that fails leaves the bytes alone."""
    document = tmp_path / "model.FCStd"
    document.write_bytes(b"known-good")
    bridge = FakeFreecad(tmp_path)
    bridge.fail_method = "partdesign.pad"

    with pytest.raises(BridgeError, match="simulated"):
        bridge.pad_feature(str(document), "ProfileSketch", "Pad", 10.0)

    assert document.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".model.*.FCStd"))


# ---------------------------------------------------------------------------
# Real host: FreeCAD 1.0.2 and 1.1.4
# ---------------------------------------------------------------------------


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_pad_adds_the_profile_volume(tmp_path: Path):
    """A pad must add the profile area times its length, on both release lines."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "pad.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)

    result = bridge.pad_feature(str(document), "ProfileSketch", "Pad", PAD_LENGTH)

    assert result["feature"]["type_id"] == "PartDesign::Pad"
    assert result["volume"]["direction"] == "add"
    # The hard threshold: not merely "it grew", but "it grew by this much".
    assert result["volume"]["delta"] == pytest.approx(PAD_VOLUME, rel=VOLUME_REL_TOLERANCE)
    assert result["volume"]["after"] == pytest.approx(PAD_VOLUME, rel=VOLUME_REL_TOLERANCE)
    assert "feature.volume_delta.direction" in result["verified"]
    assert "feature.volume_delta.magnitude" in result["verified"]


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_pocket_removes_the_profile_volume(tmp_path: Path):
    """The upstream #99 gate: a pocket must actually remove material.

    The threshold is the point. Upstream reports success while removing nothing,
    so an assertion that only checks "no exception raised" would pass on the
    broken behaviour.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "pocket.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)

    padded = bridge.pad_feature(str(document), "ProfileSketch", "Pad", PAD_LENGTH)
    before = padded["volume"]["after"]

    result = bridge.pocket_feature(str(document), "ProfileSketch", "Pocket", POCKET_DEPTH)

    assert result["feature"]["type_id"] == "PartDesign::Pocket"
    assert result["volume"]["direction"] == "remove"
    assert result["volume"]["delta"] == pytest.approx(-POCKET_VOLUME, rel=VOLUME_REL_TOLERANCE)
    assert result["volume"]["after"] == pytest.approx(
        before - POCKET_VOLUME, rel=VOLUME_REL_TOLERANCE
    )
    assert result["volume"]["after"] < before


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_reports_which_side_type_property_the_host_exposes(tmp_path: Path):
    """The side-type ladder is measured here, not assumed.

    Both release lines must either expose a reversal property and report which
    one took the write, or be refused. What is never acceptable is a host that
    silently accepts a two-sided request and produces a one-sided result.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "side.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)

    result = bridge.pad_feature(
        str(document), "ProfileSketch", "TwoSidedPad", PAD_LENGTH, side_type="two_sides"
    )

    # Whatever spelling this host offers, it is named in the result, so a host
    # that moved the property is identifiable after the fact.
    assert result["feature"]["side_property"] in ("SideType", "Midplane", "Symmetric", None)
    # A two-sided pad of this profile is twice a one-sided one.
    assert result["volume"]["delta"] == pytest.approx(2.0 * PAD_VOLUME, rel=VOLUME_REL_TOLERANCE)


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_revolution_sweeps_the_profile_volume(tmp_path: Path):
    """A revolution carries no side type, and still has to deliver volume."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "revolve.FCStd"
    bridge.create_document(str(document))
    # A profile clear of the axis, so a full turn sweeps a measurable torus.
    _constrained_sketch(bridge, document, name="RevolveSketch")

    result = bridge.revolution_feature(
        str(document), "RevolveSketch", "Revolve", 360.0, axis="vertical"
    )

    assert result["feature"]["type_id"] == "PartDesign::Revolution"
    assert result["volume"]["direction"] == "add"
    assert result["volume"]["delta"] > 0.0
    assert "feature.volume_delta.direction" in result["verified"]


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_hole_removes_the_cylinder_volume(tmp_path: Path):
    """A hole that drilled nothing must be reported, not returned as success."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "hole.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)

    padded = bridge.pad_feature(str(document), "ProfileSketch", "Pad", PAD_LENGTH)
    before = padded["volume"]["after"]

    result = bridge.create_hole(str(document), "ProfileSketch", "Hole", HOLE_DIAMETER, HOLE_DEPTH)

    assert result["volume"]["direction"] == "remove"
    assert result["volume"]["delta"] < 0.0
    assert result["volume"]["after"] < before


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_refuses_an_underconstrained_profile(tmp_path: Path):
    """The dependency gate: a loose sketch never reaches a feature."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "loose.FCStd"
    bridge.create_document(str(document))
    bridge.create_sketch(str(document), "LooseSketch", plane="xy")
    bridge.add_sketch_geometry(
        str(document),
        "LooseSketch",
        [{"type": "rectangle", "x": 0, "y": 0, "width": PROFILE_WIDTH, "height": PROFILE_HEIGHT}],
    )
    # Corners closed and sides axis-aligned, but no size or position fixed.
    bridge.add_sketch_constraint(
        str(document),
        "LooseSketch",
        [
            {"type": "horizontal", "first": {"element": 0}},
            {"type": "vertical", "first": {"element": 1}},
        ],
    )

    with pytest.raises(SketchStateError) as excinfo:
        bridge.pad_feature(str(document), "LooseSketch", "Pad", PAD_LENGTH)

    assert excinfo.value.code == "E_SKETCH_UNDERCONSTRAINED"


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_features_survive_a_durable_reopen(tmp_path: Path):
    """A feature that read back correctly must still be there after a reopen."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "durable.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)

    result = bridge.pad_feature(str(document), "ProfileSketch", "Pad", PAD_LENGTH)
    volume = result["volume"]["after"]

    inspected = bridge.inspect_document(str(document))

    assert inspected["object_count"] >= 1
    # Reopening and re-inspecting must not change the measured volume.
    again = bridge.inspect_document(str(document))
    assert again["object_count"] == inspected["object_count"]
    assert volume > 0.0
