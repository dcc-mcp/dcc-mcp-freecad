"""Sketch domain rules, tested without a FreeCAD host.

This is the fast lane for the sketch contract, and it is where the failures that
matter most are pinned: a rectangle that expands to edges that do not close, a
constraint bound to a vertex the geometry kind does not have, a sketch reported
as feature-ready on a degrees-of-freedom count nobody measured. All of those are
decisions made in Python, so they are all decidable here.
"""

from __future__ import annotations

import math

import pytest

from dcc_mcp_freecad import sketch_rules as rules

# ---------------------------------------------------------------------------
# Planes
# ---------------------------------------------------------------------------


def test_plane_normals_follow_the_declared_rotations():
    assert _close(rules.plane_normal("xy"), (0.0, 0.0, 1.0))
    assert _close(rules.plane_normal("xz"), (0.0, -1.0, 0.0))
    assert _close(rules.plane_normal("yz"), (1.0, 0.0, 0.0))


def test_every_declared_plane_has_a_unit_normal():
    for plane in rules.PLANES:
        normal = rules.plane_normal(plane)
        assert _close((math.sqrt(sum(item * item for item in normal)),), (1.0,)), plane


def test_an_unknown_plane_is_refused():
    with pytest.raises(rules.SketchSpecError, match="unsupported plane"):
        rules.plane_normal("zx")


def _close(actual, expected, tolerance=1e-9):
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_a_rectangle_expands_to_four_closing_lines():
    primitives = rules.expand_geometry(
        {"type": "rectangle", "x": 2, "y": 3, "width": 40, "height": 10}
    )

    assert [item["role"] for item in primitives] == ["bottom", "right", "top", "left"]
    assert all(item["kind"] == "line" for item in primitives)
    assert primitives[0]["points"] == [[2.0, 3.0], [42.0, 3.0]]
    assert primitives[1]["points"] == [[42.0, 3.0], [42.0, 13.0]]
    assert primitives[2]["points"] == [[42.0, 13.0], [2.0, 13.0]]
    assert primitives[3]["points"] == [[2.0, 13.0], [2.0, 3.0]]
    # The chain closes: each end is the next start, including the wrap-around.
    ends = [item["points"][1] for item in primitives]
    starts = [item["points"][0] for item in primitives]
    assert ends == starts[1:] + [starts[0]]


def test_a_line_carries_its_two_endpoints():
    primitives = rules.expand_geometry({"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 4})

    assert primitives[0]["kind"] == "line"
    assert rules.key_points(primitives[0]) == [0.0, 0.0, 10.0, 4.0]


def test_a_circle_carries_its_centre_and_radius():
    primitives = rules.expand_geometry({"type": "circle", "cx": 5, "cy": -1, "radius": 2.5})

    assert rules.key_points(primitives[0]) == [5.0, -1.0, 2.5]


def test_an_arc_carries_both_ends_its_midpoint_centre_and_radius():
    primitives = rules.expand_geometry(
        {
            "type": "arc",
            "cx": 0,
            "cy": 0,
            "radius": 10,
            "start_angle_degrees": 0,
            "end_angle_degrees": 90,
        }
    )

    arc = primitives[0]
    points = rules.key_points(arc)
    assert _close(points[0:2], (10.0, 0.0))
    assert _close(points[2:4], (7.0710678118654755, 7.0710678118654755))
    assert _close(points[4:6], (0.0, 10.0))
    assert _close(points[6:8], (0.0, 0.0))
    assert points[8] == 10.0


def test_a_point_carries_a_single_position():
    primitives = rules.expand_geometry({"type": "point", "x": 1, "y": 2})

    assert rules.key_points(primitives[0]) == [1.0, 2.0]


@pytest.mark.parametrize(
    "spec,match",
    [
        ({"type": "blob"}, "unsupported geometry type"),
        ({"type": "circle", "cx": 0, "cy": 0}, "requires radius"),
        ({"type": "circle", "cx": 0, "cy": 0, "radius": "wide"}, "must be a number"),
        ({"type": "circle", "cx": 0, "cy": 0, "radius": 0}, "radius must be positive"),
        ({"type": "circle", "cx": 0, "cy": 0, "radius": float("nan")}, "must be finite"),
        ({"type": "rectangle", "x": 0, "y": 0, "width": -1, "height": 2}, "width must be positive"),
        ({"type": "line", "x1": 0, "y1": 0, "x2": 0, "y2": 0}, "endpoints must be distinct"),
        ({"type": "point", "x": 0, "y": 0, "radius": 3}, "unsupported fields for point"),
        ("circle", "must be an object"),
    ],
)
def test_a_geometry_the_adapter_cannot_honour_is_refused(spec, match):
    with pytest.raises(rules.SketchSpecError, match=match):
        rules.expand_geometry(spec)


def test_an_arc_with_no_sweep_or_more_than_a_full_turn_is_refused():
    base = {"type": "arc", "cx": 0, "cy": 0, "radius": 1}

    with pytest.raises(rules.SketchSpecError, match="same point"):
        rules.expand_geometry(dict(base, start_angle_degrees=45, end_angle_degrees=45))
    with pytest.raises(rules.SketchSpecError, match="may not exceed 360"):
        rules.expand_geometry(dict(base, start_angle_degrees=0, end_angle_degrees=400))


@pytest.mark.parametrize("start,end", [(0, 360), (180, 540), (-90, 270)])
def test_an_arc_whose_ends_coincide_is_refused_whatever_the_sweep(start, end):
    """A full turn is a circle, not a degenerate arc.

    A 360 degree sweep passes a "sweep is non-zero" check while its endpoints
    land on the same point, so the rejection has to compare endpoints rather
    than the sweep.
    """
    with pytest.raises(rules.SketchSpecError, match="same point"):
        rules.expand_geometry(
            {
                "type": "arc",
                "cx": 0,
                "cy": 0,
                "radius": 1,
                "start_angle_degrees": start,
                "end_angle_degrees": end,
            }
        )


@pytest.mark.parametrize("start,end", [(90, 0), (0, -90), (270, 180), (1, 0)])
def test_a_descending_arc_pair_is_refused_not_reinterpreted(start, end):
    """A negative sweep is refused because the host would store its complement.

    Measured on FreeCAD 1.0.2 and 1.1.4, Part::GeomArcOfCircle keeps its
    parameter range ascending, so a descending pair is raised by a full turn: a
    90 -> 0 request stores the 270 degree arc, whose endpoints still look right
    while its midpoint lands in the opposite quadrant. Restating the pair
    expresses the same arc, so the request is refused rather than silently
    changed into the other one.
    """
    with pytest.raises(rules.SketchSpecError, match="may not be negative"):
        rules.expand_geometry(
            {
                "type": "arc",
                "cx": 0,
                "cy": 0,
                "radius": 1,
                "start_angle_degrees": start,
                "end_angle_degrees": end,
            }
        )


def test_the_refusal_names_the_ascending_pair_that_expresses_the_same_arc():
    """The error has to be actionable, not just a rejection."""
    with pytest.raises(rules.SketchSpecError) as excinfo:
        rules.expand_geometry(
            {
                "type": "arc",
                "cx": 0,
                "cy": 0,
                "radius": 1,
                "start_angle_degrees": 90,
                "end_angle_degrees": 0,
            }
        )

    message = str(excinfo.value)
    assert "ascending" in message
    # The restatement: 0 -> 90 is the same arc as the rejected 90 -> 0.
    assert "0.0 -> 90.0" in message


@pytest.mark.parametrize("start,end", [(0, 90), (90, 360), (180, 270)])
def test_an_ascending_arc_pair_is_accepted(start, end):
    """Every arc remains expressible: the refusal costs a spelling, not a shape."""
    arc = rules.expand_geometry(
        {
            "type": "arc",
            "cx": 0,
            "cy": 0,
            "radius": 1,
            "start_angle_degrees": start,
            "end_angle_degrees": end,
        }
    )[0]

    assert arc["kind"] == "arc"
    assert arc["angles_degrees"] == [start, end]


def test_the_arc_midpoint_is_part_of_the_read_back():
    """The midpoint is the only compared value that differs from a complement.

    An arc and its complement share both endpoints, centre and radius, so a
    read-back that skipped the midpoint matched a complementary sweep exactly.
    """
    arc = rules.expand_geometry(
        {
            "type": "arc",
            "cx": 0,
            "cy": 0,
            "radius": 10,
            "start_angle_degrees": 0,
            "end_angle_degrees": 90,
        }
    )[0]

    points = rules.key_points(arc)

    # start, midpoint, end, centre, radius -- radius 10 at 45 degrees
    assert len(points) == 9
    assert [round(value, 6) for value in points[2:4]] == [7.071068, 7.071068]


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_concentric_is_coincident_centres():
    constraint = rules.validate_constraint(
        {"type": "concentric", "first": {"element": 1}, "second": {"element": 2}}
    )

    assert constraint["free_cad_type"] == "Coincident"
    assert constraint["first"]["vertex"] == 3
    assert constraint["second"]["vertex"] == 3


def test_a_dimensional_constraint_keeps_its_magnitude():
    constraint = rules.validate_constraint(
        {"type": "radius", "first": {"element": 0}, "value": 12.5}
    )

    assert constraint["free_cad_type"] == "Radius"
    assert constraint["value"] == 12.5
    assert constraint["value_degrees"] is None


def test_an_angle_is_carried_in_degrees():
    constraint = rules.validate_constraint(
        {"type": "angle", "first": {"element": 0}, "second": {"element": 1}, "value_degrees": 90}
    )

    assert constraint["free_cad_type"] == "Angle"
    assert constraint["value_degrees"] == 90.0


def test_the_root_point_may_be_referenced_and_nothing_below_it():
    constraint = rules.validate_constraint(
        {
            "type": "distance_x",
            "first": {"element": -1, "vertex": 1},
            "second": {"element": 0, "vertex": 1},
            "value": 0,
        }
    )

    assert constraint["first"]["element"] == -1
    with pytest.raises(rules.SketchSpecError, match="below the sketch root point"):
        rules.validate_constraint(
            {
                "type": "distance_x",
                "first": {"element": -2, "vertex": 1},
                "second": {"element": 0, "vertex": 1},
                "value": 0,
            }
        )


@pytest.mark.parametrize(
    "spec,match",
    [
        ({"type": "blob"}, "unsupported constraint type"),
        ({"type": "horizontal"}, "requires first"),
        ({"type": "horizontal", "first": 0}, "must be an object"),
        ({"type": "horizontal", "first": {"element": 0}, "value": 3}, "unsupported fields"),
        ({"type": "horizontal", "first": {"element": "0"}}, "must be an integer"),
        ({"type": "horizontal", "first": {"vertex": 1}}, "must be an integer"),
        (
            {"type": "coincident", "first": {"element": 0}, "second": {"element": 1}},
            "requires a vertex",
        ),
        (
            {"type": "parallel", "first": {"element": 0, "vertex": 1}, "second": {"element": 1}},
            "takes no vertex",
        ),
        ({"type": "radius", "first": {"element": 0}, "value": -2}, "radius must be positive"),
        (
            {
                "type": "distance",
                "first": {"element": 0, "vertex": 1},
                "second": {"element": 1, "vertex": 1},
                "value": -1,
            },
            "must not be negative",
        ),
        (
            {"type": "angle", "first": {"element": 0}, "second": {"element": 1}},
            "requires value_degrees",
        ),
        ("horizontal", "must be an object"),
    ],
)
def test_a_constraint_the_adapter_cannot_honour_is_refused(spec, match):
    with pytest.raises(rules.SketchSpecError, match=match):
        rules.validate_constraint(spec)


def test_every_declared_constraint_type_validates_a_well_formed_spec():
    """A type the adapter advertises must be usable, not merely listed."""
    specs = {
        "coincident": {"first": {"element": 0, "vertex": 2}, "second": {"element": 1, "vertex": 1}},
        "concentric": {"first": {"element": 0}, "second": {"element": 1}},
        "point_on_object": {"first": {"element": 4, "vertex": 1}, "second": {"element": 0}},
        "horizontal": {"first": {"element": 0}},
        "vertical": {"first": {"element": 0}},
        "parallel": {"first": {"element": 0}, "second": {"element": 2}},
        "perpendicular": {"first": {"element": 0}, "second": {"element": 1}},
        "tangent": {"first": {"element": 0}, "second": {"element": 3}},
        "equal": {"first": {"element": 0}, "second": {"element": 2}},
        "distance": {
            "first": {"element": 0, "vertex": 1},
            "second": {"element": 1, "vertex": 1},
            "value": 10,
        },
        "distance_x": {
            "first": {"element": -1, "vertex": 1},
            "second": {"element": 0, "vertex": 1},
            "value": 0,
        },
        "distance_y": {
            "first": {"element": -1, "vertex": 1},
            "second": {"element": 0, "vertex": 1},
            "value": 0,
        },
        "length": {"first": {"element": 0}, "value": 40},
        "radius": {"first": {"element": 3}, "value": 4},
        "angle": {"first": {"element": 0}, "second": {"element": 1}, "value_degrees": 90},
    }
    assert set(specs) == set(rules.CONSTRAINT_TYPES)
    for name, spec in specs.items():
        constraint = rules.validate_constraint(dict(spec, type=name))
        assert constraint["type"] == name


# ---------------------------------------------------------------------------
# Constraints against real geometry kinds
# ---------------------------------------------------------------------------


def test_a_radius_on_a_line_is_refused_instead_of_reinterpreted():
    constraint = rules.validate_constraint({"type": "radius", "first": {"element": 0}, "value": 4})

    with pytest.raises(rules.SketchStateError) as excinfo:
        rules.check_kinds(constraint, "line", None)

    assert excinfo.value.code == rules.ERROR_SPEC_INVALID


def test_a_horizontal_on_a_circle_is_refused():
    constraint = rules.validate_constraint({"type": "horizontal", "first": {"element": 0}})

    with pytest.raises(rules.SketchStateError):
        rules.check_kinds(constraint, "circle", None)


def test_equal_refuses_two_different_kinds():
    constraint = rules.validate_constraint(
        {"type": "equal", "first": {"element": 0}, "second": {"element": 1}}
    )

    with pytest.raises(rules.SketchStateError, match="same kind"):
        rules.check_kinds(constraint, "line", "circle")


def test_a_vertex_the_geometry_kind_does_not_have_is_refused():
    """A circle has no start point, so vertex 1 must not be silently accepted."""
    constraint = rules.validate_constraint(
        {
            "type": "coincident",
            "first": {"element": 0, "vertex": 1},
            "second": {"element": 1, "vertex": 1},
        }
    )

    with pytest.raises(rules.SketchStateError, match="does not exist on circle"):
        rules.check_kinds(constraint, "circle", "line")


def test_the_root_point_only_offers_the_root_vertex():
    constraint = rules.validate_constraint(
        {
            "type": "coincident",
            "first": {"element": -1, "vertex": 3},
            "second": {"element": 0, "vertex": 1},
        }
    )

    with pytest.raises(rules.SketchStateError):
        rules.check_kinds(constraint, "point", "line")


def test_a_valid_constraint_passes_unharmed():
    constraint = rules.validate_constraint(
        {
            "type": "coincident",
            "first": {"element": 0, "vertex": 2},
            "second": {"element": 1, "vertex": 1},
        }
    )

    assert rules.check_kinds(constraint, "line", "line") is None


# ---------------------------------------------------------------------------
# Degrees of freedom
# ---------------------------------------------------------------------------


def test_a_fully_constrained_sketch_with_geometry_is_feature_ready():
    state = rules.feature_state(0, 4)

    assert state["feature_ready"] is True
    assert state["fully_constrained"] is True
    assert state["blocking_error_code"] is None


def test_an_empty_sketch_is_not_a_profile_whatever_the_solver_says():
    """Zero DOF with no geometry is still unusable as a pad profile."""
    state = rules.feature_state(0, 0)

    assert state["feature_ready"] is False
    assert state["blocking_error_code"] == rules.ERROR_UNDERCONSTRAINED


def test_an_underconstrained_sketch_names_its_dof():
    state = rules.feature_state(3, 4)

    assert state["dof_available"] is True
    assert state["fully_constrained"] is False
    assert state["feature_ready"] is False
    assert state["blocking_error_code"] == rules.ERROR_UNDERCONSTRAINED
    assert "3" in state["blocking_reason"]


def test_an_overconstrained_sketch_is_refused_separately():
    state = rules.feature_state(-1, 4)

    assert state["blocking_error_code"] == rules.ERROR_OVERCONSTRAINED
    assert state["feature_ready"] is False


def test_an_unmeasured_dof_is_never_reported_as_constrained():
    """The host that hides the number must not be answered with a zero."""
    state = rules.feature_state(None, 4)

    assert state["dof"] is None
    assert state["dof_available"] is False
    assert state["fully_constrained"] is False
    assert state["feature_ready"] is False
    assert state["blocking_error_code"] == rules.ERROR_DOF_UNAVAILABLE


def test_assert_feature_ready_raises_a_structured_error_code():
    state = rules.feature_state(2, 4)

    with pytest.raises(rules.SketchStateError) as excinfo:
        rules.assert_feature_ready(state, "FlangeSketch", "sketch.info")

    error = excinfo.value
    assert error.code == rules.ERROR_UNDERCONSTRAINED
    assert error.state_payload["code"] == rules.ERROR_UNDERCONSTRAINED
    assert error.state_payload["details"]["sketch_name"] == "FlangeSketch"
    assert "FlangeSketch" in str(error)


def test_assert_feature_ready_passes_a_ready_sketch_through():
    assert rules.assert_feature_ready(rules.feature_state(0, 4), "S", "sketch.info")[
        "feature_ready"
    ]


def test_the_state_payload_survives_a_json_round_trip():
    import json

    error = rules.SketchStateError(rules.ERROR_UNDERCONSTRAINED, "blocked", dof=2, sketch_name="S")

    assert json.loads(json.dumps(error.state_payload)) == error.state_payload


def test_every_blocking_code_is_declared():
    for code in (
        rules.ERROR_UNDERCONSTRAINED,
        rules.ERROR_OVERCONSTRAINED,
        rules.ERROR_DOF_UNAVAILABLE,
        rules.ERROR_SOLVER_FAILED,
    ):
        assert code in rules.FEATURE_BLOCKING_CODES
