"""The sketch driver against a fake FreeCAD host.

This is the lane where the sketch contract is proved to *fire*. The fake host
stores what it is told, so a write the host drops is observably missing, and a
constraint bound to an element that does not exist is refusable before the
first write rather than after the fact. The real-host lane in
``tests/test_sketch_bridge.py`` proves the same checks do not fire on real
geometry.

The fake deliberately does not model a solver: degrees of freedom are whatever
the test says the host reported. Modelling a solver would mean the tests
validated the fake's arithmetic instead of the adapter's refusal to invent a
number it was never given.
"""

from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import (  # noqa: E402
    freecad_driver,
    sketch_rules,
    write_contract,
)

HOST_VERSION = "1.1.4"

SKETCH_ID = "Sketcher::SketchObject"
BODY_ID = "PartDesign::Body"
PLANE_ID = "PartDesign::Plane"


# ---------------------------------------------------------------------------
# A FreeCAD that stores what it is told
# ---------------------------------------------------------------------------


class _Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __repr__(self):
        return "Vector(%r, %r, %r)" % (self.x, self.y, self.z)


def _rodrigues(vector, axis, angle):
    cosine = math.cos(angle)
    sine = math.sin(angle)
    norm = math.sqrt(sum(item * item for item in axis)) or 1.0
    kx, ky, kz = (item / norm for item in axis)
    vx, vy, vz = vector
    dot = kx * vx + ky * vy + kz * vz
    cross = (ky * vz - kz * vy, kz * vx - kx * vz, kx * vy - ky * vx)
    return _Vec(
        vx * cosine + cross[0] * sine + kx * dot * (1.0 - cosine),
        vy * cosine + cross[1] * sine + ky * dot * (1.0 - cosine),
        vz * cosine + cross[2] * sine + kz * dot * (1.0 - cosine),
    )


class _Rotation:
    """Mirrors ``App.Rotation(axis, angle)``, whose angle is in degrees."""

    def __init__(self, axis=None, angle=0.0):
        self.Axis = axis if axis is not None else _Vec(0.0, 0.0, 1.0)
        self.Angle = math.radians(float(angle))

    def multVec(self, vector):
        return _rodrigues(
            (vector.x, vector.y, vector.z), (self.Axis.x, self.Axis.y, self.Axis.z), self.Angle
        )

    def __repr__(self):
        return "Rotation(%r, %r)" % (self.Axis, self.Angle)


class _Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base if base is not None else _Vec()
        self.Rotation = rotation if rotation is not None else _Rotation()

    def __repr__(self):
        return "Placement(%r, %r)" % (self.Base, self.Rotation)


class _Geom:
    def __init__(self, type_id, **fields):
        self.TypeId = type_id
        for key, value in fields.items():
            setattr(self, key, value)

    def value(self, parameter):
        """The point at a parameter on the curve, as the host reports it.

        Only arcs need it: the read-back uses the midpoint of the parameter
        range to tell an arc apart from its complement, which share both
        endpoints, centre and radius.

        A reversed arc is evaluated on a flipped basis (Reverse() negates the
        conic's Z axis, so the Y direction flips with it). The fake models the
        measured host behaviour, where the range is always ascending, so no arc
        it builds is reversed.
        """
        return _Vec(
            self.Center.x + self.Radius * math.cos(parameter),
            self.Center.y + self.Radius * math.sin(parameter),
            0.0,
        )


class _Constraint:
    """Mirrors the positional layouts of ``Sketcher::Constraint``."""

    _LAYOUTS = {
        "Coincident": ("First", "FirstPos", "Second", "SecondPos"),
        "PointOnObject": ("First", "FirstPos", "Second"),
        "Horizontal": ("First",),
        "Vertical": ("First",),
        "Parallel": ("First", "Second"),
        "Perpendicular": ("First", "Second"),
        "Tangent": ("First", "Second"),
        "Equal": ("First", "Second"),
        "DistanceX": ("First", "FirstPos", "Second", "SecondPos", "Value"),
        "DistanceY": ("First", "FirstPos", "Second", "SecondPos", "Value"),
        "Radius": ("First", "Value"),
        "Angle": ("First", "Second", "Value"),
    }

    def __init__(self, type_name, *args):
        self.Type = type_name
        self.First = 0
        self.FirstPos = 0
        self.Second = 0
        self.SecondPos = 0
        self.Third = 0
        self.Value = 0.0
        layout = self._LAYOUTS.get(type_name)
        if layout is None:
            # Distance is the one constructor whose arity changes its meaning:
            # three arguments pin an element's length, six measure between two
            # points. The adapter separates them into two constraint types, so
            # the fake has to keep them apart too.
            layout = (
                ("First", "Value")
                if len(args) == 2
                else ("First", "FirstPos", "Second", "SecondPos", "Value")
            )
        for name, value in zip(layout, args):
            setattr(self, name, value)


class _Object:
    def __init__(self, type_id, name):
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        self.Placement = _Placement()
        self.Shape = None
        self.OutList = ()
        self.InList = []

    def newObject(self, type_id, name):
        raise AttributeError("only a PartDesign body holds features")


class _Plane(_Object):
    def __init__(self, name):
        super().__init__(PLANE_ID, name)


class _Body(_Object):
    def __init__(self, name, doc):
        super().__init__(BODY_ID, name)
        self._doc = doc

    def newObject(self, type_id, name):
        return self._doc._add(type_id, name, self)


class _Sketch(_Object):
    """A sketch that stores writes, unless silenced.

    ``_silenced`` models the failure class the read-back exists for: the host
    accepts the attachment and returns, but the sketch is never actually placed
    on its plane.
    """

    def __init__(self, name, attachment_property="AttachmentSupport"):
        super().__init__(SKETCH_ID, name)
        self.Geometry = []
        self.Constraints = []
        # Real FreeCAD exposes ``AttachmentSupport``; the legacy ``Support``
        # spelling is what older hosts offer, and the adapter probes for both.
        self.attachment_property = attachment_property
        setattr(self, attachment_property, [])
        self.MapMode = "Deactivated"
        self.ExternalGeometry = []
        self.Shape = None
        # Real FreeCAD reports the count on `DoF`; `solve()` only reports
        # failure and returns 0 for anything that solves.
        self.DoF = 0
        self.solve_error = None
        self.drop_geometry = 0
        self.drop_constraints = False
        self._silenced = False

    def __setattr__(self, key, value):
        if getattr(self, "_silenced", False) and key in ("AttachmentSupport", "Support", "MapMode"):
            return
        object.__setattr__(self, key, value)

    def addGeometry(self, geometries, construction=False):
        if not isinstance(geometries, list):
            geometries = [geometries]
        self.Geometry.extend(geometries[: len(geometries) - self.drop_geometry])

    def addConstraint(self, constraint):
        if self.drop_constraints:
            return
        self.Constraints.append(constraint)

    def getConstruction(self, index):
        return False

    def solve(self):
        if self.solve_error is not None:
            raise RuntimeError(self.solve_error)
        return 0


class _Document:
    def __init__(self, name, path="", app=None):
        self.Name = name
        self.Label = name
        self.FileName = path
        self.Objects = []
        self.silence_new = False
        # Which attachment spelling this host offers, so a test can pin that a
        # host exposing only the legacy name is still driven correctly.
        self.attachment_property = "AttachmentSupport"
        self._app = app

    def addObject(self, type_id, name):
        return self._add(type_id, name, None)

    def _add(self, type_id, name, parent):
        if type_id == BODY_ID:
            obj = _Body(name, self)
        elif type_id == PLANE_ID:
            obj = _Plane(name)
        elif type_id == SKETCH_ID:
            obj = _Sketch(name, self.attachment_property)
            if self.silence_new:
                obj._silenced = True
        else:
            obj = _Object(type_id, name)
        if parent is not None:
            obj.InList.append(parent)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name):
        obj = self.getObject(name)
        if obj is None:
            raise ValueError("No such object: %s" % name)
        self.Objects = [item for item in self.Objects if item.Name != name]

    def recompute(self):
        # A sketch placed by its support: the host derives the placement from
        # the attachment, so an attachment that did not land leaves the sketch
        # at the identity placement and shows up as the wrong plane normal.
        for obj in self.Objects:
            if obj.TypeId == SKETCH_ID:
                support = getattr(obj, obj.attachment_property, None)
                if support:
                    obj.Placement = support[0][0].Placement

    def save(self):
        pass

    def saveAs(self, path):
        self.FileName = path
        Path(path).write_bytes(b"fake-fcstd")


class _App:
    def __init__(self, version=HOST_VERSION):
        major, minor, patch = (version.split(".") + ["0", "0"])[:3]
        self.Version = lambda: [major, minor, patch, "extra"]
        self.documents = {}
        self.Vector = _Vec
        self.Rotation = _Rotation
        self.Placement = _Placement

    def newDocument(self, name):
        doc = _Document(name, "", self)
        self.documents[name] = doc
        return doc

    def openDocument(self, path):
        key = str(path)
        if key not in self.documents:
            self.documents[key] = _Document(Path(key).stem, key, self)
        return self.documents[key]

    def closeDocument(self, name):
        # The fake keeps the document registered so a multi-step test can keep
        # working on it; the driver never depends on a closed handle dying.
        self.closed = getattr(self, "closed", []) + [name]


class FakePart(types.ModuleType):
    def __init__(self):
        super().__init__("Part")
        self.shift = 0.0

    def Point(self, vector):
        return _Geom("Part::GeomPoint", X=vector.x + self.shift, Y=vector.y + self.shift)

    def LineSegment(self, start, end):
        return _Geom(
            "Part::GeomLineSegment",
            StartPoint=_Vec(start.x + self.shift, start.y + self.shift, 0.0),
            EndPoint=_Vec(end.x + self.shift, end.y + self.shift, 0.0),
        )

    def Circle(self, center, normal, radius):
        return _Geom(
            "Part::GeomCircle",
            Center=_Vec(center.x + self.shift, center.y + self.shift, 0.0),
            Radius=radius,
        )

    def ArcOfCircle(self, circle, start_angle, end_angle, sense=True):
        """Model the host's arc storage as the real hosts were measured to behave.

        Part::GeomArcOfCircle keeps its parameter range ascending: when the end
        angle is below the start angle, the upper bound is raised by a full turn
        rather than the pair being swapped. Measured identically on FreeCAD 1.0.2
        and 1.1.4, a request for 90 -> 0 therefore stores a 270 degree arc from
        90 to 360 whose midpoint sits at 225 degrees, not the 90 degree arc
        through 45 that was asked for.

        The fake reproduces that measurement rather than a reading of
        Geom_TrimmedCurve::SetTrim. An earlier version modelled the source
        instead of the hosts, agreed with the code under test and let the defect
        through with 554 local tests green -- a fake that is built from the same
        wrong theory as the implementation cannot catch the implementation.
        """
        last = end_angle if end_angle >= start_angle else end_angle + 2 * math.pi
        cx, cy = circle.Center.x, circle.Center.y
        return _Geom(
            "Part::GeomArcOfCircle",
            Center=circle.Center,
            Radius=circle.Radius,
            FirstParameter=start_angle,
            LastParameter=last,
            StartPoint=_Vec(
                cx + circle.Radius * math.cos(start_angle),
                cy + circle.Radius * math.sin(start_angle),
                0.0,
            ),
            EndPoint=_Vec(
                cx + circle.Radius * math.cos(last),
                cy + circle.Radius * math.sin(last),
                0.0,
            ),
        )


class FakeSketcher(types.ModuleType):
    def __init__(self):
        super().__init__("Sketcher")
        self.Constraint = _Constraint


@pytest.fixture()
def host(monkeypatch):
    """Install a fake FreeCAD and return the harness.

    The driver loads its sibling modules by path, so seeding the cache with the
    imported modules makes the error types a test catches the same objects the
    driver raises.
    """
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_write_contract"] = write_contract
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_sketch_rules"] = sketch_rules
    app = _App()
    part = FakePart()
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "Part", part)
    monkeypatch.setitem(sys.modules, "Sketcher", FakeSketcher())
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    return types.SimpleNamespace(app=app, part=part)


def _document(host, tmp_path, name="model"):
    path = tmp_path / ("%s.FCStd" % name)
    path.write_bytes(b"document")
    return host.app.openDocument(str(path)), str(path)


def _create(host, path, name="Sketch", plane="xy", **extra):
    params = {"document_path": path, "name": name, "plane": plane}
    params.update(extra)
    return freecad_driver.sketch_create(params)


def _sketch(host, path, name):
    return host.app.openDocument(path).getObject(name)


def _state_error(excinfo, code):
    error = excinfo.value
    assert isinstance(error, sketch_rules.SketchStateError), type(error)
    assert error.code == code, error.code
    return error


# ---------------------------------------------------------------------------
# sketch.create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plane", ["xy", "xz", "yz"])
def test_create_sketch_attaches_to_the_requested_plane(host, tmp_path, plane):
    _doc, path = _document(host, tmp_path)

    result = _create(host, path, plane=plane)

    assert result["sketch"]["plane"] == plane
    assert result["sketch"]["type_id"] == SKETCH_ID
    assert result["body"]["name"] == "Body"
    assert result["body"]["created"] is True
    assert result["attachment"]["plane_object"] == "SketchPlane"
    assert _close(result["sketch"]["normal"], sketch_rules.plane_normal(plane))
    for check in (
        "sketch.exists",
        "sketch.type_id",
        "sketch.support",
        "sketch.support_plane",
        "sketch.map_mode",
        "sketch.plane_normal",
        "sketch.in_body",
    ):
        assert check in result["verified"], check


def test_create_sketch_reuses_an_existing_body(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject(BODY_ID, "Body")

    result = _create(host, path)

    assert result["body"]["created"] is False
    assert "sketch.in_body" in result["verified"]


def test_create_sketch_refuses_a_name_that_is_not_a_body(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")

    with pytest.raises(ValueError, match="not a PartDesign body"):
        _create(host, path)


def test_create_sketch_refuses_a_duplicate_name(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)

    with pytest.raises(ValueError, match="already exists"):
        _create(host, path)


def test_create_sketch_refuses_an_unsupported_plane(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="unsupported plane"):
        _create(host, path, plane="zx")


def test_create_sketch_refuses_when_the_attachment_never_placed_it(host, tmp_path):
    """A sketch with a support but no derived placement is on the wrong plane."""
    doc, path = _document(host, tmp_path)
    doc.recompute = lambda: None  # the placement never follows the attachment

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        _create(host, path, plane="xz")

    assert excinfo.value.check == "sketch.plane_normal"


def test_create_sketch_refuses_when_the_support_write_was_dropped(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.silence_new = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        _create(host, path)

    assert excinfo.value.check == "sketch.support"


def test_create_sketch_drives_a_host_that_only_has_the_legacy_attachment(host, tmp_path):
    """`AttachmentSupport` was `Support` once; the adapter probes, not assumes."""
    doc, path = _document(host, tmp_path)
    doc.attachment_property = "Support"

    result = _create(host, path, plane="yz")

    assert result["attachment"]["property"] == "Support"
    assert "sketch.support" in result["verified"]
    assert _close(result["sketch"]["normal"], sketch_rules.plane_normal("yz"))


def test_create_sketch_refuses_a_host_with_no_attachment_property(host, tmp_path):
    """An unattached sketch would draw in an arbitrary plane and report success."""
    doc, path = _document(host, tmp_path)
    doc.attachment_property = "NothingHere"

    with pytest.raises(freecad_driver.IncompatibleHostError) as excinfo:
        _create(host, path)

    assert "AttachmentSupport" in str(excinfo.value)
    assert "Support" in str(excinfo.value)


def test_a_fresh_sketch_is_not_reported_as_a_usable_profile(host, tmp_path):
    """Zero DOF on an empty sketch is not a profile, whatever the solver says."""
    _doc, path = _document(host, tmp_path)

    result = _create(host, path)

    assert result["dof"] == 0
    assert result["feature_state"]["feature_ready"] is False
    assert result["feature_state"]["blocking_error_code"] == sketch_rules.ERROR_UNDERCONSTRAINED


# ---------------------------------------------------------------------------
# sketch.add_geometry
# ---------------------------------------------------------------------------


def test_add_geometry_returns_one_index_per_element(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)

    result = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "geometry": [{"type": "rectangle", "x": 0, "y": 0, "width": 40, "height": 10}],
        }
    )

    assert [item["index"] for item in result["elements"]] == [0, 1, 2, 3]
    assert [item["role"] for item in result["elements"]] == ["bottom", "right", "top", "left"]
    assert result["elements"][0]["key_points"] == [0.0, 0.0, 40.0, 0.0]
    assert result["sketch"]["geometry_count"] == 4
    for check in ("geometry.count", "geometry[0].kind", "geometry[0].points"):
        assert check in result["verified"], check


def test_add_geometry_appends_after_existing_elements(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "geometry": [{"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0}],
        }
    )

    result = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "geometry": [{"type": "circle", "cx": 2, "cy": 3, "radius": 4}],
        }
    )

    assert [item["index"] for item in result["elements"]] == [1]
    assert result["elements"][0]["key_points"] == [2.0, 3.0, 4.0]


@pytest.mark.parametrize("start,end,mid_degrees", [(0, 90, 45.0), (90, 360, 225.0)])
def test_the_stored_arc_is_the_sweep_that_was_asked_for(host, tmp_path, start, end, mid_degrees):
    """Assert the stored curve, not the argument the driver passed.

    An earlier version of this test asserted only that ``sense`` was True. That
    pinned the means instead of the end: sense *was* True, the test passed, and
    the arc was still stored as its complement on both real hosts. What matters
    is the curve that comes back -- its endpoints and, decisively, its midpoint,
    which is the only one of the three that differs from the complement.
    """
    _doc, path = _document(host, tmp_path)
    _create(host, path, name="Profile")

    added = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": [
                {
                    "type": "arc",
                    "cx": 0,
                    "cy": 0,
                    "radius": 10,
                    "start_angle_degrees": start,
                    "end_angle_degrees": end,
                }
            ],
        }
    )

    def at(degrees):
        angle = math.radians(degrees)
        return (10 * math.cos(angle), 10 * math.sin(angle))

    points = added["elements"][0]["key_points"]
    assert _close(points[0:2], at(start)), "the start point is the requested start angle"
    assert _close(points[2:4], at(mid_degrees)), (
        "the arc sweeps through %s degrees, not its complement" % mid_degrees
    )
    assert _close(points[4:6], at(end % 360)), "the end point is the requested end angle"


def test_a_negative_arc_sweep_is_refused_with_a_restatement_rule(host, tmp_path):
    """A descending pair is refused, and the error says how to write it instead.

    The host would store the complement of a negative sweep while its endpoints
    still looked correct, so the request is refused rather than reinterpreted.
    The refusal has to be actionable: an agent told only "unsupported" cannot
    recover, whereas the restatement rule costs it one rewritten pair.
    """
    _doc, path = _document(host, tmp_path)
    _create(host, path, name="Profile")

    with pytest.raises(sketch_rules.SketchSpecError) as excinfo:
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "geometry": [
                    {
                        "type": "arc",
                        "cx": 0,
                        "cy": 0,
                        "radius": 10,
                        "start_angle_degrees": 90,
                        "end_angle_degrees": 0,
                    }
                ],
            }
        )

    message = str(excinfo.value)
    assert "may not be negative" in message
    # The restatement rule: the same arc written as an ascending pair.
    assert "0" in message and "90" in message
    assert "ascending" in message


def test_an_arc_restating_a_negative_sweep_is_accepted(host, tmp_path):
    """The rejection is recoverable: the restated pair stores the same arc.

    This is what makes refusing a negative sweep lose no expressive power -- the
    90 degree arc through 45 degrees is still available, written 0 -> 90.
    """
    _doc, path = _document(host, tmp_path)
    _create(host, path, name="Profile")

    added = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": [
                {
                    "type": "arc",
                    "cx": 0,
                    "cy": 0,
                    "radius": 10,
                    "start_angle_degrees": 0,
                    "end_angle_degrees": 90,
                }
            ],
        }
    )

    points = added["elements"][0]["key_points"]
    assert _close(points[2:4], (7.0710678118654755, 7.0710678118654755))


def test_add_geometry_refuses_an_arc_whose_midpoint_cannot_be_read(host, tmp_path):
    """An arc whose midpoint is unreadable is refused, not compared without it.

    Dropping the midpoint from the comparison would be the same bug in a new
    coat: the check would pass on every arc, including a reversed one.
    """
    _doc, path = _document(host, tmp_path)
    _create(host, path, name="Profile")

    def blind_value(_parameter):
        raise RuntimeError("no midpoint on this host")

    monkeypatched = host.part.ArcOfCircle

    def arc_without_midpoint(circle, start, end, sense=True):
        geometry = monkeypatched(circle, start, end, sense)
        geometry.value = blind_value
        return geometry

    host.part.ArcOfCircle = arc_without_midpoint

    with pytest.raises(Exception, match="midpoint"):
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "geometry": [
                    {
                        "type": "arc",
                        "cx": 0,
                        "cy": 0,
                        "radius": 10,
                        "start_angle_degrees": 0,
                        "end_angle_degrees": 90,
                    }
                ],
            }
        )


def test_a_negative_solver_status_is_refused_not_reported_as_constrained(host, tmp_path):
    """A solver that did not converge is a failure, even when DOF reads zero.

    The host leaves the geometry un-updated and ``FullyConstrained`` unset on a
    negative status, so a sketch reporting dof 0 with status -2 is not the
    solved profile the caller asked for.
    """
    _doc, path = _document(host, tmp_path)
    _create(host, path, name="Profile")
    sketch = _sketch(host, path, "Profile")
    sketch.DoF = 0
    sketch.solve = lambda: -2

    with pytest.raises(sketch_rules.SketchStateError, match="did not converge"):
        freecad_driver._sketch_dof(sketch, "1.1.4", "sketch.add_geometry")


def test_add_geometry_proves_the_arc_it_was_asked_for(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)

    result = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "geometry": [
                {
                    "type": "arc",
                    "cx": 0,
                    "cy": 0,
                    "radius": 10,
                    "start_angle_degrees": 0,
                    "end_angle_degrees": 90,
                }
            ],
        }
    )

    assert result["elements"][0]["kind"] == "arc"
    points = result["elements"][0]["key_points"]
    # start, midpoint, end, centre, radius -- radius 10 at 45 degrees
    assert _close(points[0:2], (10.0, 0.0))
    assert _close(points[2:4], (7.0710678118654755, 7.0710678118654755))
    assert _close(points[4:6], (0.0, 10.0))
    assert _close(points[6:8], (0.0, 0.0))
    assert points[8] == 10.0


def test_add_geometry_refuses_when_the_host_dropped_an_element(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _sketch(host, path, "Sketch").drop_geometry = 1

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Sketch",
                "geometry": [{"type": "rectangle", "x": 0, "y": 0, "width": 40, "height": 10}],
            }
        )

    assert excinfo.value.check == "geometry.count"
    assert excinfo.value.expected == 4
    assert excinfo.value.actual == 3


def test_add_geometry_refuses_coordinates_that_did_not_land(host, tmp_path):
    doc, path = _document(host, tmp_path)
    _create(host, path)
    host.part.shift = 0.5

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Sketch",
                "geometry": [{"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}],
            }
        )

    assert excinfo.value.check == "geometry[0].points"
    assert excinfo.value.actual == [0.5, 0.5, 10.5, 0.5]


def test_add_geometry_refuses_a_spec_the_rules_reject(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)

    with pytest.raises(sketch_rules.SketchSpecError, match="must be positive"):
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Sketch",
                "geometry": [{"type": "circle", "cx": 0, "cy": 0, "radius": -2}],
            }
        )


def test_add_geometry_refuses_an_empty_batch(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)

    with pytest.raises(ValueError, match="non-empty list"):
        freecad_driver.sketch_add_geometry(
            {"document_path": path, "sketch_name": "Sketch", "geometry": []}
        )


def test_add_geometry_refuses_an_object_that_is_not_a_sketch(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "NotASketch")

    with pytest.raises(ValueError, match="not a sketch"):
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "NotASketch",
                "geometry": [{"type": "point", "x": 0, "y": 0}],
            }
        )


# ---------------------------------------------------------------------------
# sketch.add_constraint
# ---------------------------------------------------------------------------


def _rectangle(host, path, name="Sketch"):
    return freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": name,
            "geometry": [{"type": "rectangle", "x": 0, "y": 0, "width": 40, "height": 10}],
        }
    )


def test_add_constraint_stores_what_it_was_asked_for(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)

    result = freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "constraints": [
                {"type": "horizontal", "first": {"element": 0}},
                {"type": "length", "first": {"element": 0}, "value": 40},
                {
                    "type": "coincident",
                    "first": {"element": 0, "vertex": 2},
                    "second": {"element": 1, "vertex": 1},
                },
            ],
        }
    )

    assert [item["type"] for item in result["constraints"]] == [
        "Horizontal",
        "Distance",
        "Coincident",
    ]
    assert result["constraints"][1]["value"] == 40.0
    assert result["sketch"]["constraint_count"] == 3
    for check in ("constraint.count", "constraint[0].type", "constraint[1].value"):
        assert check in result["verified"], check


def test_add_constraint_reports_the_dof_change(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    _sketch(host, path, "Sketch").DoF = 6

    result = freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "constraints": [{"type": "horizontal", "first": {"element": 0}}],
        }
    )

    assert result["dof"]["before"] == 6
    assert result["dof"]["after"] == 6
    assert result["dof"]["source"] == "property.DoF"
    assert result["feature_state"]["feature_ready"] is False


def test_add_constraint_refuses_an_element_that_does_not_exist(host, tmp_path):
    """A dangling reference is refused before the first write, not after it."""
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Sketch",
                "constraints": [{"type": "horizontal", "first": {"element": 9}}],
            }
        )

    error = _state_error(excinfo, sketch_rules.ERROR_ELEMENT_NOT_FOUND)
    assert error.state_payload["details"]["element"] == 9
    assert error.state_payload["details"]["geometry_count"] == 4
    # Nothing was written: the refusal happened before addConstraint ran.
    assert _sketch(host, path, "Sketch").Constraints == []


def test_add_constraint_refuses_a_dimension_on_the_wrong_geometry(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "geometry": [{"type": "circle", "cx": 0, "cy": 0, "radius": 5}],
        }
    )

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Sketch",
                "constraints": [{"type": "horizontal", "first": {"element": 0}}],
            }
        )

    _state_error(excinfo, sketch_rules.ERROR_SPEC_INVALID)


def test_add_constraint_converts_degrees_to_radians_and_reads_them_back(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "geometry": [
                {"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0},
                {"type": "line", "x1": 0, "y1": 0, "x2": 0, "y2": 10},
            ],
        }
    )

    result = freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "constraints": [
                {
                    "type": "angle",
                    "first": {"element": 0},
                    "second": {"element": 1},
                    "value_degrees": 90,
                }
            ],
        }
    )

    assert result["constraints"][0]["value"] == math.radians(90)
    assert "constraint[0].value" in result["verified"]


def test_add_constraint_refuses_when_the_host_dropped_one(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    _sketch(host, path, "Sketch").drop_constraints = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Sketch",
                "constraints": [{"type": "horizontal", "first": {"element": 0}}],
            }
        )

    assert excinfo.value.check == "constraint.count"
    assert excinfo.value.actual == 0


def test_add_constraint_refuses_a_spec_the_rules_reject(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)

    with pytest.raises(sketch_rules.SketchSpecError, match="requires second"):
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Sketch",
                "constraints": [{"type": "coincident", "first": {"element": 0, "vertex": 2}}],
            }
        )


# ---------------------------------------------------------------------------
# sketch.info
# ---------------------------------------------------------------------------


def test_info_reports_geometry_constraints_and_dof(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Sketch",
            "constraints": [{"type": "horizontal", "first": {"element": 0}}],
        }
    )

    result = freecad_driver.sketch_info({"document_path": path, "sketch_name": "Sketch"})

    assert result["sketch"]["name"] == "Sketch"
    assert result["sketch"]["body"] == "Body"
    assert result["sketch"]["plane"] == "xy"
    assert len(result["geometry"]) == 4
    assert [item["kind"] for item in result["geometry"]] == ["line"] * 4
    assert [item["type"] for item in result["constraints"]] == ["Horizontal"]
    assert result["external_geometry_count"] == 0
    assert result["dof"] == 0
    assert result["feature_state"]["feature_ready"] is True


def test_info_refuses_an_underconstrained_sketch_when_readiness_is_required(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    _sketch(host, path, "Sketch").DoF = 5

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        freecad_driver.sketch_info(
            {"document_path": path, "sketch_name": "Sketch", "require_fully_constrained": True}
        )

    error = _state_error(excinfo, sketch_rules.ERROR_UNDERCONSTRAINED)
    assert error.state_payload["details"]["dof"] == 5
    assert "Sketch" in str(error)


def test_info_reports_an_underconstrained_sketch_without_readiness_by_default(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    _sketch(host, path, "Sketch").DoF = 5

    result = freecad_driver.sketch_info({"document_path": path, "sketch_name": "Sketch"})

    assert result["dof"] == 5
    assert result["feature_state"]["fully_constrained"] is False
    assert result["feature_state"]["feature_ready"] is False
    assert result["feature_state"]["blocking_error_code"] == sketch_rules.ERROR_UNDERCONSTRAINED


def test_info_never_claims_readiness_on_an_unmeasured_dof(host, tmp_path):
    """A host that hides the number must not be answered with a zero."""
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    del _sketch(host, path, "Sketch").DoF

    result = freecad_driver.sketch_info({"document_path": path, "sketch_name": "Sketch"})

    assert result["dof"] is None
    assert result["dof_source"] is None
    assert result["feature_state"]["feature_ready"] is False
    assert result["feature_state"]["blocking_error_code"] == sketch_rules.ERROR_DOF_UNAVAILABLE


def test_a_solver_failure_is_reported_instead_of_absorbed(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    _sketch(host, path, "Sketch").solve_error = "over-constrained"

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        freecad_driver.sketch_info({"document_path": path, "sketch_name": "Sketch"})

    error = _state_error(excinfo, sketch_rules.ERROR_SOLVER_FAILED)
    assert "over-constrained" in error.state_payload["details"]["solver_error"]


def test_the_dof_count_is_never_read_from_solve(host, tmp_path):
    """`solve()` returns 0 for anything that solves, on both supported hosts.

    Reading that 0 as a count reports every sketch as fully constrained, which
    is the exact failure this gate exists to prevent: the number is plausible,
    measured, and wrong. The fake reproduces the real return value.
    """
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    _sketch(host, path, "Sketch").DoF = 4

    result = freecad_driver.sketch_info({"document_path": path, "sketch_name": "Sketch"})

    assert result["dof"] == 4, result
    assert result["dof_source"] == "property.DoF"
    assert result["feature_state"]["feature_ready"] is False


def test_a_host_that_renames_the_dof_property_is_still_measured(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    _create(host, path)
    _rectangle(host, path)
    sketch = _sketch(host, path, "Sketch")
    del sketch.DoF
    sketch.DOF = 2

    result = freecad_driver.sketch_info({"document_path": path, "sketch_name": "Sketch"})

    assert result["dof"] == 2
    assert result["dof_source"] == "property.DOF"


def test_info_refuses_an_object_that_is_not_a_sketch(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Box")

    with pytest.raises(ValueError, match="not a sketch"):
        freecad_driver.sketch_info({"document_path": path, "sketch_name": "Box"})


# ---------------------------------------------------------------------------
# Host capability probing
# ---------------------------------------------------------------------------


class _StubFeature:
    def __init__(self, **properties):
        for key, value in properties.items():
            setattr(self, key, value)


def test_the_reversal_property_is_resolved_newest_spelling_first():
    assert (
        freecad_driver._resolve_reversal_property(
            _StubFeature(Symmetric=True, SideType="Two"), "1.1.4"
        )
        == "SideType"
    )
    assert (
        freecad_driver._resolve_reversal_property(
            _StubFeature(Symmetric=True, Midplane=True), "1.1.4"
        )
        == "Midplane"
    )
    assert (
        freecad_driver._resolve_reversal_property(_StubFeature(Symmetric=True), "1.0.2")
        == "Symmetric"
    )


def test_the_reversal_property_refuses_a_host_with_none_of_the_spellings():
    with pytest.raises(freecad_driver.IncompatibleHostError) as excinfo:
        freecad_driver._resolve_reversal_property(_StubFeature(), "1.1.4")

    message = str(excinfo.value)
    for name in ("SideType", "Midplane", "Symmetric"):
        assert name in message, name
    assert "refused instead of silently doing nothing" in message


# ---------------------------------------------------------------------------
# The error reaches the caller with its structure intact
# ---------------------------------------------------------------------------


def test_the_driver_forwards_the_sketch_error_code(host, tmp_path):
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    path = tmp_path / "d.FCStd"
    path.write_bytes(b"document")
    _create(host, str(path))
    _rectangle(host, str(path))
    _sketch(host, str(path), "Sketch").DoF = 3
    request.write_text(
        json.dumps(
            {
                "method": "sketch.info",
                "params": {
                    "document_path": str(path),
                    "sketch_name": "Sketch",
                    "require_fully_constrained": True,
                },
            }
        ),
        encoding="utf-8",
    )
    sys.argv = ["driver", "--pass", str(request), str(result)]

    freecad_driver.main()

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == sketch_rules.ERROR_UNDERCONSTRAINED
    assert payload["error"]["sketch_state"]["details"]["dof"] == 3


def test_the_driver_forwards_a_dangling_element_reference(host, tmp_path):
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    path = tmp_path / "d.FCStd"
    path.write_bytes(b"document")
    _create(host, str(path))
    request.write_text(
        json.dumps(
            {
                "method": "sketch.add_constraint",
                "params": {
                    "document_path": str(path),
                    "sketch_name": "Sketch",
                    "constraints": [{"type": "horizontal", "first": {"element": 4}}],
                },
            }
        ),
        encoding="utf-8",
    )
    sys.argv = ["driver", "--pass", str(request), str(result)]

    freecad_driver.main()

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == sketch_rules.ERROR_ELEMENT_NOT_FOUND


def _close(actual, expected, tolerance=1e-9):
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))
