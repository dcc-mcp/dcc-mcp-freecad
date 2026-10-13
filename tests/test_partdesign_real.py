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

import math
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


def _rectangle_constraints(width=PROFILE_WIDTH, height=PROFILE_HEIGHT, x=0, y=0):
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
        {"type": "length", "first": {"element": 0}, "value": width},
        {"type": "length", "first": {"element": 1}, "value": height},
        {
            "type": "distance_x",
            "first": {"element": -1, "vertex": 1},
            "second": {"element": 0, "vertex": 1},
            "value": x,
        },
        {
            "type": "distance_y",
            "first": {"element": -1, "vertex": 1},
            "second": {"element": 0, "vertex": 1},
            "value": y,
        },
    ]


def _constrained_sketch(
    bridge, document, name="ProfileSketch", width=PROFILE_WIDTH, height=PROFILE_HEIGHT, x=0, y=0
):
    """Create a fully constrained 40 x 10 mm rectangle and return its info."""
    bridge.create_sketch(str(document), name, plane="xy")
    bridge.add_sketch_geometry(
        str(document),
        name,
        [{"type": "rectangle", "x": x, "y": y, "width": width, "height": height}],
    )
    bridge.add_sketch_constraint(str(document), name, _rectangle_constraints(width, height, x, y))
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

    _constrained_sketch(bridge, document, name="CutSketch")
    result = bridge.pocket_feature(
        str(document), "CutSketch", "Pocket", POCKET_DEPTH, reversed=True
    )

    assert result["feature"]["type_id"] == "PartDesign::Pocket"
    assert result["volume"]["direction"] == "remove"
    assert result["volume"]["delta"] == pytest.approx(-POCKET_VOLUME, rel=VOLUME_REL_TOLERANCE)
    assert result["volume"]["after"] == pytest.approx(
        before - POCKET_VOLUME, rel=VOLUME_REL_TOLERANCE
    )
    assert result["volume"]["after"] < before


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
@pytest.mark.parametrize("side_type,factor", [("two_sides", 2), ("symmetric", 1)])
def test_real_freecad_reports_which_side_type_property_the_host_exposes(
    tmp_path: Path, side_type, factor
):
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
        str(document), "ProfileSketch", "TwoSidedPad", PAD_LENGTH, side_type=side_type
    )

    # Whatever spelling this host offers, it is named in the result, so a host
    # that moved the property is identifiable after the fact.
    assert result["feature"]["side_property"] in ("SideType", "Midplane", "Symmetric", None)
    # A two-sided pad of this profile is twice a one-sided one.
    assert result["volume"]["delta"] == pytest.approx(factor * PAD_VOLUME, rel=VOLUME_REL_TOLERANCE)


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
@pytest.mark.parametrize("axis,radius", [("vertical", 20), ("horizontal", 5)])
def test_real_freecad_revolution_sweeps_the_profile_volume(tmp_path: Path, axis, radius):
    """A revolution carries no side type, and still has to deliver volume."""
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "revolve.FCStd"
    bridge.create_document(str(document))
    # The rectangle touches the chosen axis, so a full turn forms a cylinder.
    _constrained_sketch(bridge, document, name="RevolveSketch")

    result = bridge.revolution_feature(str(document), "RevolveSketch", "Revolve", 360.0, axis=axis)

    assert result["feature"]["type_id"] == "PartDesign::Revolution"
    assert result["volume"]["direction"] == "add"
    assert result["volume"]["delta"] == pytest.approx(PROFILE_AREA * 2 * math.pi * radius)
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

    bridge.create_sketch(str(document), "HoleSketch", plane="xy")
    bridge.add_sketch_geometry(
        str(document),
        "HoleSketch",
        [{"type": "circle", "cx": 20, "cy": 5, "radius": HOLE_DIAMETER / 2}],
    )
    bridge.add_sketch_constraint(
        str(document),
        "HoleSketch",
        [
            {"type": "radius", "first": {"element": 0}, "value": HOLE_DIAMETER / 2},
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
    _host_script(
        bridge,
        tmp_path,
        "place_hole",
        f"""
import FreeCAD as App
doc = App.openDocument({str(document)!r})
doc.HoleSketchPlane.Placement.Base.z = {PAD_LENGTH}
doc.recompute()
doc.save()
App.closeDocument(doc.Name)
""",
    )
    result = bridge.create_hole(str(document), "HoleSketch", "Hole", HOLE_DIAMETER, HOLE_DEPTH)

    assert result["volume"]["direction"] == "remove"
    assert result["volume"]["delta"] == pytest.approx(-HOLE_VOLUME, rel=VOLUME_REL_TOLERANCE)
    assert result["volume"]["after"] == pytest.approx(before - HOLE_VOLUME)


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

    bridge.pad_feature(str(document), "ProfileSketch", "Pad", PAD_LENGTH)
    _constrained_sketch(bridge, document, name="CutSketch", width=10, height=4, x=10, y=3)
    cut = bridge.pocket_feature(str(document), "CutSketch", "Pocket", 4, reversed=True)
    assert cut["volume"]["after"] == pytest.approx(3840)

    # Exercise native dimensional editing through the existing script-file tool.
    # No frozen replacement solid: both parametric features must recompute.
    _host_script(
        bridge,
        tmp_path,
        "edit_dimensions",
        f"""
import FreeCAD as App
doc = App.openDocument({str(document)!r})
doc.ProfileSketch.setDatum(8, App.Units.Quantity('50 mm'))
doc.Pad.Length = 12
doc.Pocket.Length = 5
doc.recompute()
assert doc.ProfileSketch.DoF == 0
assert abs(doc.Pocket.Shape.Volume - 5800) < 1e-6
doc.save()
App.closeDocument(doc.Name)
""",
    )
    for _ in range(2):
        inspected = bridge.inspect_document(str(document))
        objects = {obj["name"]: obj for obj in inspected["objects"]}
        assert objects["Pad"]["type_id"] == "PartDesign::Pad"
        assert objects["Pocket"]["type_id"] == "PartDesign::Pocket"
        assert objects["Body"]["shape"]["volume"] == pytest.approx(5800)
        assert objects["Pocket"]["shape"]["valid"]
        assert objects["Pocket"]["shape"]["solids"] == 1

    step = tmp_path / "edited.step"
    bridge.export_geometry(str(document), ["Body"], str(step))
    imported = tmp_path / "step_readback.FCStd"
    bridge.create_document(str(imported))
    result = bridge.import_geometry(str(imported), str(step), "RoundTrip")
    assert result["object"]["shape"]["volume"] == pytest.approx(5800)
    assert result["object"]["shape"]["solids"] == 1
    assert result["object"]["shape"]["valid"]


def _host_script(bridge, root, name, source):
    script = root / (name + ".py")
    script.write_text(source, encoding="utf-8")
    result = bridge.run_script(str(script))
    assert result["exit_code"] == 0, result


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_loft_uses_document_section_links(tmp_path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "loft.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)
    _constrained_sketch(bridge, document, name="TopSketch")
    _host_script(
        bridge,
        tmp_path,
        "move_section",
        f"""
import FreeCAD as App
doc = App.openDocument({str(document)!r})
doc.TopSketchPlane.Placement.Base.z = 20
doc.recompute()
doc.save()
App.closeDocument(doc.Name)
""",
    )
    result = bridge.loft_feature(
        str(document), "ProfileSketch", ["ProfileSketch", "TopSketch"], "Loft", 20
    )
    assert result["volume"]["delta"] == pytest.approx(PROFILE_AREA * 20)
    objects = {obj["name"]: obj for obj in bridge.inspect_document(str(document))["objects"]}
    assert objects["Loft"]["shape"]["volume"] == pytest.approx(PROFILE_AREA * 20)


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_sweep_uses_document_spine_link(tmp_path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "sweep.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)
    _host_script(
        bridge,
        tmp_path,
        "create_spine",
        f"""
import FreeCAD as App
import Part
import Sketcher
doc = App.openDocument({str(document)!r})
sketch = doc.Body.newObject('Sketcher::SketchObject', 'PathSketch')
sketch.Placement = App.Placement(App.Vector(0, 0, 0), App.Rotation(App.Vector(1, 0, 0), 90))
sketch.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(0, 20, 0)), False)
sketch.addConstraint(Sketcher.Constraint('Block', 0))
doc.recompute()
assert sketch.DoF == 0
doc.save()
App.closeDocument(doc.Name)
""",
    )
    result = bridge.sweep_feature(str(document), "ProfileSketch", "PathSketch", "Sweep", 20)
    assert result["volume"]["delta"] == pytest.approx(PROFILE_AREA * 20)
    objects = {obj["name"]: obj for obj in bridge.inspect_document(str(document))["objects"]}
    assert objects["Sweep"]["shape"]["volume"] == pytest.approx(PROFILE_AREA * 20)


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_groove_removes_analytic_volume(tmp_path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "groove.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)
    base = bridge.revolution_feature(str(document), "ProfileSketch", "Revolve", 360)
    _constrained_sketch(bridge, document, name="CutSketch", x=10, y=2, width=10, height=5)
    result = bridge.groove_feature(str(document), "CutSketch", "Groove", 360)
    removed = 1500 * math.pi
    assert result["volume"]["delta"] == pytest.approx(-removed)
    assert result["volume"]["after"] == pytest.approx(base["volume"]["after"] - removed)


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_zero_cut_preserves_source_bytes(tmp_path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "zero_cut.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)
    bridge.pad_feature(str(document), "ProfileSketch", "Pad", PAD_LENGTH)
    _constrained_sketch(bridge, document, name="CutSketch")
    before = document.read_bytes()
    with pytest.raises(BridgeError, match="volume_delta.direction"):
        bridge.pocket_feature(str(document), "CutSketch", "Pocket", POCKET_DEPTH)
    assert document.read_bytes() == before
    assert not list(tmp_path.glob(".zero_cut.*.FCStd"))


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_nested_profile_subtracts_inner_wire(tmp_path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "ring.FCStd"
    bridge.create_document(str(document))
    bridge.create_sketch(str(document), "RingSketch")
    bridge.add_sketch_geometry(
        str(document),
        "RingSketch",
        [
            {"type": "circle", "cx": 0, "cy": 0, "radius": 10},
            {"type": "circle", "cx": 0, "cy": 0, "radius": 5},
        ],
    )
    bridge.add_sketch_constraint(
        str(document),
        "RingSketch",
        [
            {"type": "radius", "first": {"element": 0}, "value": 10},
            {"type": "radius", "first": {"element": 1}, "value": 5},
            {
                "type": "coincident",
                "first": {"element": 0, "vertex": 3},
                "second": {"element": -1, "vertex": 1},
            },
            {
                "type": "coincident",
                "first": {"element": 1, "vertex": 3},
                "second": {"element": -1, "vertex": 1},
            },
        ],
    )
    result = bridge.pad_feature(str(document), "RingSketch", "Ring", 10)
    area = math.pi * (10**2 - 5**2)
    assert result["profile_area"] == pytest.approx(area)
    assert result["volume"]["delta"] == pytest.approx(area * 10)


@pytest.mark.freecad
@pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set")
def test_real_freecad_two_sided_pocket_cuts_both_full_extents(tmp_path):
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "two_sided_pocket.FCStd"
    bridge.create_document(str(document))
    _constrained_sketch(bridge, document)
    bridge.pad_feature(str(document), "ProfileSketch", "Pad", 20, side_type="symmetric")
    _constrained_sketch(bridge, document, name="CutSketch", width=10, height=4, x=10, y=3)
    result = bridge.pocket_feature(str(document), "CutSketch", "Pocket", 4, side_type="two_sides")
    assert result["volume"]["expected_delta"] == pytest.approx(-320)
    assert result["volume"]["delta"] == pytest.approx(-320)
    assert result["volume"]["after"] == pytest.approx(7680)
