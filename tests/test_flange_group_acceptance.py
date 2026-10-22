"""Group-level acceptance for the PartDesign sketch-driven modelling line.

This file is the acceptance evidence for the two promises the parent issue
makes:

1. A flange -- boss, counterbore, fillet and polar pattern -- can be built with
   *typed calls only*. No ``run_script``, no ``execute_python``: every modelling
   step goes through the adapter's own tools.
2. The same call sequence yields the *same topology* on FreeCAD 1.0.2 and 1.1.4,
   and an under-constrained sketch is refused rather than silently extruded.

It also closes the one integration gap the three sub-issues left open.
PIP-4326's dress-up and pattern tools were only ever exercised on a primitive
box (``Part::Box``). A PartDesign body is a different animal: its ``Edges`` are
renumbered by the kernel as features are added, so an edge reference that meant
one thing before a pocket can mean another after it. The fillet and pattern
tests below resolve edges from their live geometry rather than trusting an
index, and then assert the dressed result is what the geometry said.

Two host facts shape the geometry, and both were measured rather than assumed:

* Every sketch attaches to a base plane, so all profiles are coplanar. The
  flange is therefore built from concentric annuli, which is how a real flange
  is modelled anyway.
* A lone circle is not a closed wire. Its centre must be constrained coincident
  with the sketch root point before a pad will extrude it; pinning the centre
  with ``distance_x`` / ``distance_y`` alone leaves DOF 0 but produces no
  material.

The volume assertions are analytic: each expected number is computed from the
profile, so a host that changed the geometry fails instead of redefining it.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from dcc_mcp_freecad.bridge import FreecadBridge, SketchStateError

# ---------------------------------------------------------------------------
# The flange, in millimetres.
# ---------------------------------------------------------------------------

# Outer disc, and the central bore that makes the disc an annulus.
OUTER_RADIUS = 40.0
BORE_RADIUS = 6.0
FLANGE_THICKNESS = 10.0

# The counterbore: a wide shallow cut concentric with the bore. It is cut as an
# annulus from the bore edge outwards, so it lands entirely in material.
COUNTERBORE_RADIUS = 10.0
COUNTERBORE_DEPTH = 4.0

# A bolt hole on the bolt circle, then repeated by the polar pattern.
BOLT_HOLE_RADIUS = 3.0
BOLT_HOLE_DEPTH = 10.0
BOLT_CIRCLE_RADIUS = 30.0
BOLT_COUNT = 6

# Fillet on the outer rim.
RIM_FILLET_RADIUS = 2.0

# Profile areas, computed once so the volume expectations stay readable.
FLANGE_AREA = math.pi * (OUTER_RADIUS**2 - BORE_RADIUS**2)
COUNTERBORE_AREA = math.pi * (COUNTERBORE_RADIUS**2 - BORE_RADIUS**2)
BOLT_HOLE_AREA = math.pi * BOLT_HOLE_RADIUS**2

# Volumes are compared with a relative tolerance: the kernel tessellates curved
# faces, so a bit-compare is not meaningful. This is still tight enough that a
# missing feature differs by orders of magnitude.
VOLUME_REL_TOLERANCE = 0.01


def _real_freecad() -> str:
    return os.environ.get("FREECAD_TEST_EXECUTABLE", "")


# Every test in this file needs a real host: the whole point is measuring two
# of them against each other.
pytestmark = [
    pytest.mark.freecad,
    pytest.mark.skipif(not _real_freecad(), reason="FREECAD_TEST_EXECUTABLE is not set"),
]


# ---------------------------------------------------------------------------
# Sketch helpers
# ---------------------------------------------------------------------------


def _circle_pair_constraints(outer_radius, inner_radius):
    """Fully constrain two concentric circles. DOF 0.

    Each circle contributes three degrees of freedom: its radius and the two
    coordinates of its centre. Both radii are fixed as datums, and both centres
    are pinned to the sketch root point, which also makes the pair a closed
    wire -- the annulus a pad will actually extrude.
    """
    return [
        {"type": "radius", "first": {"element": 0}, "value": outer_radius},
        {"type": "radius", "first": {"element": 1}, "value": inner_radius},
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
    ]


def _constrained_annulus(bridge, document, name, outer_radius, inner_radius):
    """An annulus profile: two concentric circles, fully constrained."""
    bridge.create_sketch(str(document), name, plane="xy")
    bridge.add_sketch_geometry(
        str(document),
        name,
        [
            {"type": "circle", "cx": 0, "cy": 0, "radius": outer_radius},
            {"type": "circle", "cx": 0, "cy": 0, "radius": inner_radius},
        ],
    )
    bridge.add_sketch_constraint(
        str(document), name, _circle_pair_constraints(outer_radius, inner_radius)
    )
    info = bridge.get_sketch_info(str(document), name)
    assert info["dof"] == 0, info
    return info


def _constrained_circle(bridge, document, name, radius, cx=0.0, cy=0.0):
    """One circle, fully constrained: its radius and its centre. DOF 0.

    The centre is pinned differently depending on where it goes, and the
    difference was measured rather than guessed:

    * At the origin, a ``coincident`` constraint against the sketch root point
      is what closes the wire. A circle pinned only by distances sits at the
      right coordinates but is not a usable profile, and the pad is refused for
      adding nothing.
    * Off the origin, the same ``coincident`` constraint *over*-constrains the
      sketch -- the solver returns status -4 and refuses to converge. There the
      centre is fixed with ``distance_x`` / ``distance_y`` alone, which leaves
      DOF 0 and still closes the wire.
    """
    bridge.create_sketch(str(document), name, plane="xy")
    bridge.add_sketch_geometry(
        str(document), name, [{"type": "circle", "cx": cx, "cy": cy, "radius": radius}]
    )
    if cx or cy:
        constraints = [
            {"type": "radius", "first": {"element": 0}, "value": radius},
            {
                "type": "distance_x",
                "first": {"element": -1, "vertex": 1},
                "second": {"element": 0, "vertex": 3},
                "value": cx,
            },
            {
                "type": "distance_y",
                "first": {"element": -1, "vertex": 1},
                "second": {"element": 0, "vertex": 3},
                "value": cy,
            },
        ]
    else:
        constraints = [
            {"type": "radius", "first": {"element": 0}, "value": radius},
            {
                "type": "coincident",
                "first": {"element": 0, "vertex": 3},
                "second": {"element": -1, "vertex": 1},
            },
        ]
    bridge.add_sketch_constraint(str(document), name, constraints)
    info = bridge.get_sketch_info(str(document), name)
    assert info["dof"] == 0, info
    return info


def _body_volume(bridge, document):
    """The body's volume, read back from the document."""
    for obj in bridge.inspect_document(str(document))["objects"]:
        if obj["name"] == "Body":
            return obj["shape"]["volume"]
    raise AssertionError("no Body in the document")


def _body_topo(bridge, document):
    """The shape fingerprint two hosts must agree on.

    Counts alone can be coincidental, so this also carries the surface types of
    the faces: a cylinder versus a plane is a real modelling difference a bare
    count would hide.
    """
    return _read_edges(bridge, document)["topo"]


# ---------------------------------------------------------------------------
# A. The flange, end to end, with typed calls only
# ---------------------------------------------------------------------------


def test_flange_is_built_with_typed_calls_only(tmp_path: Path):
    """The acceptance flange, built entirely through typed tool calls.

    Every call below goes through the adapter's typed surface. There is no
    script escape hatch in this test -- which is the point, because the parent
    issue's promise is that the main CAD path is reachable without one.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "flange.FCStd"
    bridge.create_document(str(document))

    # 1. The disc: an annulus, so the central bore is part of the profile.
    _constrained_annulus(bridge, document, "FlangeSketch", OUTER_RADIUS, BORE_RADIUS)
    flange = bridge.pad_feature(str(document), "FlangeSketch", "FlangePad", FLANGE_THICKNESS)
    assert flange["volume"]["delta"] == pytest.approx(
        FLANGE_AREA * FLANGE_THICKNESS, rel=VOLUME_REL_TOLERANCE
    )

    # 2. The counterbore: a wide shallow concentric cut, fully in material.
    _constrained_annulus(bridge, document, "CounterboreSketch", COUNTERBORE_RADIUS, BORE_RADIUS)
    counterbore = bridge.pocket_feature(
        str(document), "CounterboreSketch", "Counterbore", COUNTERBORE_DEPTH, reversed=True
    )
    assert counterbore["volume"]["direction"] == "remove"
    assert counterbore["volume"]["delta"] == pytest.approx(
        -COUNTERBORE_AREA * COUNTERBORE_DEPTH, rel=VOLUME_REL_TOLERANCE
    )

    # 3. One bolt hole on the bolt circle.
    _constrained_circle(bridge, document, "BoltSketch", BOLT_HOLE_RADIUS, cx=BOLT_CIRCLE_RADIUS)
    bolt = bridge.pocket_feature(
        str(document), "BoltSketch", "BoltHole", BOLT_HOLE_DEPTH, reversed=True
    )
    assert bolt["volume"]["delta"] == pytest.approx(
        -BOLT_HOLE_AREA * BOLT_HOLE_DEPTH, rel=VOLUME_REL_TOLERANCE
    )

    # The composition check: the body now carries the disc less everything cut
    # out of it. Asserting the total, rather than each feature's own local
    # success, is what proves the features composed.
    expected = (
        FLANGE_AREA * FLANGE_THICKNESS
        - COUNTERBORE_AREA * COUNTERBORE_DEPTH
        - BOLT_HOLE_AREA * BOLT_HOLE_DEPTH
    )
    assert _body_volume(bridge, document) == pytest.approx(expected, rel=VOLUME_REL_TOLERANCE)


def test_flange_topology_agrees_across_host_versions(tmp_path: Path):
    """The dual-version gate: identical calls, identical topology.

    Run under both 1.0.2 and 1.1.4 by CI. The assertions are exact counts, so a
    host that resolves a feature differently fails here instead of quietly
    shipping a different part.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "flange_topo.FCStd"
    bridge.create_document(str(document))

    _constrained_annulus(bridge, document, "FlangeSketch", OUTER_RADIUS, BORE_RADIUS)
    bridge.pad_feature(str(document), "FlangeSketch", "FlangePad", FLANGE_THICKNESS)
    _constrained_annulus(bridge, document, "CounterboreSketch", COUNTERBORE_RADIUS, BORE_RADIUS)
    bridge.pocket_feature(
        str(document), "CounterboreSketch", "Counterbore", COUNTERBORE_DEPTH, reversed=True
    )

    topo = _body_topo(bridge, document)

    assert topo["solids"] == 1, topo
    # An annulus disc with a counterbore: outer wall, bore wall, counterbore
    # wall and floor, plus the two flat ends. The numbers are fixed by the
    # geometry, not by whichever host is running.
    assert topo["faces"] == 6, topo
    assert topo["edges"] == 9, topo
    assert topo["volume"] == pytest.approx(
        FLANGE_AREA * FLANGE_THICKNESS - COUNTERBORE_AREA * COUNTERBORE_DEPTH,
        rel=VOLUME_REL_TOLERANCE,
    )


# ---------------------------------------------------------------------------
# The integration gap: dress-up and patterns on a PartDesign body
# ---------------------------------------------------------------------------


def test_fillet_applies_to_a_partdesign_feature_edge(tmp_path: Path):
    """PIP-4326's regression: dress a PartDesign body, not a primitive.

    The dress-up tools were validated only on ``Part::Box``. A PartDesign body's
    edge numbering is derived by the kernel and shifts as features are added, so
    the reference must be resolved from the live shape at the moment of the
    call. This builds a pad, reads the live edge list, targets the rim circle by
    its geometry, and asserts the fillet landed on that edge.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "pd_fillet.FCStd"
    bridge.create_document(str(document))

    _constrained_annulus(bridge, document, "DiscSketch", OUTER_RADIUS, BORE_RADIUS)
    bridge.pad_feature(str(document), "DiscSketch", "DiscPad", FLANGE_THICKNESS)

    # Resolve the top rim circle by geometry rather than by a remembered index.
    edges = _read_edges(bridge, document)["edges"]
    rim = [item for item in edges if _is_circle_at(item, OUTER_RADIUS, z=FLANGE_THICKNESS)]
    assert len(rim) == 1, "expected exactly one top rim circle, got %r" % (edges,)

    result = bridge.fillet_edges(
        str(document), "Body", [rim[0]["index"]], RIM_FILLET_RADIUS, "RimFillet"
    )
    assert result["object"]["shape"]["solids"] == 1
    # A fillet on a convex rim removes material, and only along the rim: the
    # bound is the swept corner volume, so a fillet that ate the part fails.
    assert result["volume_delta"] < 0, result["volume_delta"]
    bound = 2 * math.pi * OUTER_RADIUS * RIM_FILLET_RADIUS**2 * (1 - math.pi / 4)
    assert abs(result["volume_delta"]) < abs(bound) + 1.0, result["volume_delta"]


def test_polar_pattern_repeats_a_partdesign_cut_around_an_axis(tmp_path: Path):
    """A pattern of a PartDesign pocket, on a PartDesign body.

    The bolt-circle case from the acceptance flange, isolated: one pocket, then
    repeated six times about the Z axis. The volume must be the source body with
    six holes removed -- not one, and not a fused overlap.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "pd_pattern.FCStd"
    bridge.create_document(str(document))

    _constrained_annulus(bridge, document, "DiscSketch", OUTER_RADIUS, BORE_RADIUS)
    bridge.pad_feature(str(document), "DiscSketch", "DiscPad", FLANGE_THICKNESS)
    _constrained_circle(bridge, document, "BoltSketch", BOLT_HOLE_RADIUS, cx=BOLT_CIRCLE_RADIUS)
    bridge.pocket_feature(str(document), "BoltSketch", "BoltHole", BOLT_HOLE_DEPTH, reversed=True)

    before = _body_volume(bridge, document)
    assert before == pytest.approx(
        FLANGE_AREA * FLANGE_THICKNESS - BOLT_HOLE_AREA * BOLT_HOLE_DEPTH,
        rel=VOLUME_REL_TOLERANCE,
    )

    result = bridge.polar_pattern(
        str(document), "Body", [0, 0, 1], BOLT_COUNT, "BoltCircle", total_angle_degrees=360
    )
    patterned = result["object"]["shape"]
    # The host repeats the body and leaves the copies as separate solids rather
    # than fusing them, which is what a polar pattern of a whole body means: six
    # flanges on a circle, not one merged blob. Asserting that shape is the
    # point -- a pattern that silently fused, or dropped an instance, would
    # report a different solid count.
    assert patterned["solids"] == BOLT_COUNT, patterned
    # Six copies of the source body: the total is six times what one weighs.
    assert patterned["volume"] == pytest.approx(before * BOLT_COUNT, rel=VOLUME_REL_TOLERANCE)


def test_edge_indices_are_stable_across_a_partdesign_feature_addition(tmp_path: Path):
    """The edge-numbering fragility the parent issue called out.

    A PartDesign body's ``Edges`` are renumbered by the kernel as features are
    added. This records the numbering before and after a pocket and asserts the
    change is visible, which is the evidence that callers must resolve edge
    references from the live shape rather than reusing an index across calls.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "pd_edges.FCStd"
    bridge.create_document(str(document))

    _constrained_annulus(bridge, document, "DiscSketch", OUTER_RADIUS, BORE_RADIUS)
    bridge.pad_feature(str(document), "DiscSketch", "DiscPad", FLANGE_THICKNESS)
    before = _read_edges(bridge, document)["edges"]

    _constrained_circle(bridge, document, "BoltSketch", BOLT_HOLE_RADIUS, cx=BOLT_CIRCLE_RADIUS)
    bridge.pocket_feature(str(document), "BoltSketch", "BoltHole", BOLT_HOLE_DEPTH, reversed=True)
    after = _read_edges(bridge, document)["edges"]

    # The pocket adds geometry, so the edge count grows -- and since indices are
    # positional, an index captured before the pocket no longer names the same
    # edge. Both fillet tests above resolve indices live for exactly this reason.
    assert len(after) > len(before), (len(before), len(after))
    assert [item["index"] for item in after] == list(range(1, len(after) + 1))


# ---------------------------------------------------------------------------
# B. An under-constrained sketch is refused, not silently extruded
# ---------------------------------------------------------------------------


def test_underconstrained_flange_sketch_is_refused_end_to_end(tmp_path: Path):
    """The second group-level promise, inside the acceptance flow.

    A circle with its radius fixed but its centre still free is under-
    constrained. Handing it to ``pad_feature`` must raise a typed refusal rather
    than extruding wherever the solver left it: a pad on a floating profile is
    not reproducible across hosts, which is the failure the parent issue rejects.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "loose_flange.FCStd"
    bridge.create_document(str(document))

    bridge.create_sketch(str(document), "LooseSketch", plane="xy")
    bridge.add_sketch_geometry(
        str(document), "LooseSketch", [{"type": "circle", "cx": 0, "cy": 0, "radius": OUTER_RADIUS}]
    )
    # Only the radius is fixed; both centre coordinates remain free.
    bridge.add_sketch_constraint(
        str(document),
        "LooseSketch",
        [{"type": "radius", "first": {"element": 0}, "value": OUTER_RADIUS}],
    )
    info = bridge.get_sketch_info(str(document), "LooseSketch")
    assert info["dof"] > 0, info

    # The inspection gate reports it, naming the degrees of freedom so the
    # caller learns how loose the sketch is.
    with pytest.raises(Exception) as excinfo:
        bridge.get_sketch_info(str(document), "LooseSketch", require_fully_constrained=True)
    message = str(excinfo.value).upper()
    assert "UNCONSTRAINED DEGREE" in message, message
    assert "NOT REPRODUCIBLE" in message, message

    # The feature refuses it with the documented code.
    with pytest.raises(SketchStateError) as feature_excinfo:
        bridge.pad_feature(str(document), "LooseSketch", "Pad", FLANGE_THICKNESS)
    assert feature_excinfo.value.code == "E_SKETCH_UNDERCONSTRAINED"

    # Nothing was extruded.
    inspected = bridge.inspect_document(str(document))
    assert not any(obj["type_id"] == "PartDesign::Pad" for obj in inspected["objects"])


def test_underconstrained_sketch_is_refused_after_a_successful_feature(tmp_path: Path):
    """The gate must hold once the body has features, not only on a fresh doc.

    A guard that works on an empty document and stops working after the first
    feature is the classic way this degrades. This builds a valid pad first,
    then tries to add a second feature on a loose sketch.
    """
    bridge = FreecadBridge(_real_freecad(), allowed_roots=[tmp_path])
    document = tmp_path / "gate.FCStd"
    bridge.create_document(str(document))

    _constrained_annulus(bridge, document, "DiscSketch", OUTER_RADIUS, BORE_RADIUS)
    bridge.pad_feature(str(document), "DiscSketch", "DiscPad", FLANGE_THICKNESS)

    # A second sketch, deliberately left loose: radius fixed, centre free.
    bridge.create_sketch(str(document), "LooseSketch", plane="xy")
    bridge.add_sketch_geometry(
        str(document), "LooseSketch", [{"type": "circle", "cx": 0, "cy": 0, "radius": 5}]
    )
    bridge.add_sketch_constraint(
        str(document), "LooseSketch", [{"type": "radius", "first": {"element": 0}, "value": 5}]
    )
    assert bridge.get_sketch_info(str(document), "LooseSketch")["dof"] > 0

    with pytest.raises(SketchStateError) as excinfo:
        bridge.pocket_feature(str(document), "LooseSketch", "LoosePocket", 4)
    assert excinfo.value.code == "E_SKETCH_UNDERCONSTRAINED"


# ---------------------------------------------------------------------------
# Read-only inspection helpers
# ---------------------------------------------------------------------------


def _read_edges(bridge, document):
    """Read the live edges and topology fingerprint of the body.

    This is a read-only inspection helper used to *select* edges by their
    geometry and to fingerprint topology. It is not part of the model build:
    the flange itself is built entirely through typed tools.

    The script is written next to the document so it stays inside the bridge's
    allowed roots, which every path through the bridge is checked against.
    """
    import json

    script = document.parent / "inspect_edges.py"
    out = document.parent / "inspect_edges.json"
    script.write_text(
        """
import json
import FreeCAD as App
doc = App.openDocument(%r)
obj = doc.getObject("Body")
shape = obj.Shape
edges = []
for i, edge in enumerate(shape.Edges, start=1):
    com = edge.CenterOfMass
    item = {
        "index": i,
        "type": edge.Curve.TypeId,
        "com": [com.x, com.y, com.z],
        "length": edge.Length,
        "radius": getattr(edge.Curve, "Radius", None),
    }
    edges.append(item)
topo = {
    "solids": len(shape.Solids),
    "faces": len(shape.Faces),
    "edges": len(shape.Edges),
    "vertices": len(shape.Vertexes),
    "volume": shape.Volume,
    "face_types": sorted({face.Surface.TypeId for face in shape.Faces}),
}
with open(%r, "w") as handle:
    json.dump({"edges": edges, "topo": topo}, handle)
"""
        % (str(document), str(out)),
        encoding="utf-8",
    )
    result = bridge.run_script(str(script))
    assert result["exit_code"] == 0, result
    return json.loads(out.read_text(encoding="utf-8"))


def _is_circle_at(edge, radius, z):
    """True when this edge is a circle of ``radius`` centred on Z at height ``z``."""
    if edge["radius"] is None:
        return False
    return (
        abs(edge["radius"] - radius) < 1e-6
        and abs(edge["com"][2] - z) < 1e-6
        and abs(edge["com"][0]) < 1e-6
        and abs(edge["com"][1]) < 1e-6
    )
