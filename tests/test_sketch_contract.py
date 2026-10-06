"""Sketch read-back contract: constraints are first-class and DOF is measured.

Two failure modes are under test, both of them the "reported success, model
unchanged" shape this contract exists to eliminate:

* a sketch write the host accepted but did not store, so the caller is given
  element ids for geometry that does not exist;
* an under-constrained sketch handed over as if it were finished, which would
  let a feature be built on a profile the solver is still free to move.

The fake host stores what it is told and can be told to drop writes, so both are
observable. The real-host lane in ``tests/test_bridge.py`` is what proves these
guards do not fire on correct sketches; this file proves they do fire.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import (  # noqa: E402
    freecad_driver,
    write_contract,
)

HOST_VERSION = "1.1.4"


# ---------------------------------------------------------------------------
# A Sketcher stand-in that stores what it is told
# ---------------------------------------------------------------------------


class _Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class _Constraint:
    """One stored sketch constraint.

    ``Type`` is stored as the string that was constructed, mirroring the real
    host: on FreeCAD 1.0 the C++ ``type2str`` table is missing ``Coincident`` and
    shifts every string by one, so the driver deliberately never routes a type
    back through that path.
    """

    def __init__(self, kind, arguments):
        self.Type = kind
        self.arguments = arguments
        self.First = arguments[0] if len(arguments) > 0 else None
        self.FirstPos = arguments[1] if len(arguments) > 1 else None
        self.Second = arguments[2] if len(arguments) > 2 else None
        self.SecondPos = arguments[3] if len(arguments) > 3 else None
        # A dimensional constraint carries its driving value in the last slot,
        # after however many index/position pairs its overload takes. The count
        # is not fixed at 3 or 5: Radius, Diameter and a single-element Distance
        # are built from one index plus the value, so deciding by arity alone
        # would report those as carrying no value at all.
        dimensional = any(
            kind == free_cad_name
            for free_cad_name, _arity in freecad_driver._DIMENSIONAL_CONSTRAINTS.values()
        )
        self.Value = arguments[-1] if dimensional and arguments else None
        # A Distance built from a single element (index, value) has no second
        # reference at all, and the host records that as GeoUndef (-2000) rather
        # than as a reference to element 0 -- which would mean "the distance from
        # this element's first point to itself" and read back as zero length.
        if dimensional and len(arguments) == 2:
            self.Second = -2000


class _LineSegment:
    def __init__(self, start, end):
        self.StartPoint = start
        self.EndPoint = end


class _Point:
    def __init__(self, location):
        self.Location = location


class _Circle:
    def __init__(self, center, radius):
        self.Center = center
        self.Radius = radius


class _Sketch:
    """A Sketcher::SketchObject stand-in.

    Stores geometry and constraints, and tracks degrees of freedom the way the
    real solver does: every element adds freedom and every constraint removes
    some, so DOF falls to zero only once the profile is actually pinned down.
    ``silenced`` models the bug class this contract exists for -- the host accepts
    the write and returns, but nothing changes.
    """

    def __init__(self, type_id, name, doc):
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        self.AttachmentSupport = ()
        self.MapMode = "Deactivated"
        self.Geometry = []
        self.Constraints = []
        self.DoF = 0
        self.ConflictingConstraints = []
        self.RedundantConstraints = []
        self._silenced = False
        self._doc = doc
        self._solved = False

    def solve(self):
        self._solved = True
        # Two degrees of freedom per unconstrained element, one removed per
        # constraint: crude, but it makes DOF observable without a solver.
        self.DoF = max(0, 2 * len(self.Geometry) - len(self.Constraints))
        return 0

    def addGeometry(self, geometry):
        if self._silenced:
            # The write was accepted and dropped: no element is stored.
            return 0 if not isinstance(geometry, (list, tuple)) else tuple()
        items = geometry if isinstance(geometry, (list, tuple)) else [geometry]
        start = len(self.Geometry)
        self.Geometry.extend(items)
        ids = tuple(range(start, start + len(items)))
        return ids if len(items) > 1 else ids[0]

    def addConstraint(self, constraint):
        if self._silenced:
            return 0
        self.Constraints.append(constraint)
        return len(self.Constraints) - 1


class _SketchBody:
    """Stands in for a PartDesign::Body, which owns its sketches."""

    def __init__(self, doc, name):
        self.TypeId = "PartDesign::Body"
        self.Name = name
        self.Label = name
        self._doc = doc

    def newObject(self, type_id, name):
        return self._doc.addObject(type_id, name)


class _Document:
    def __init__(self, name, path="", app=None):
        self.Name = name
        self.Label = name
        self.FileName = path
        self.Objects = []
        self._app = app
        self._silenced = False

    def addObject(self, type_id, name):
        if type_id == "PartDesign::Body":
            obj = _SketchBody(self, name)
        elif type_id == "Sketcher::SketchObject":
            obj = _Sketch(type_id, name, self)
            obj._silenced = self._silenced
        else:
            obj = _Object(type_id, name, self)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def recompute(self):
        pass

    def save(self):
        pass

    def saveAs(self, path):
        self.FileName = path
        Path(path).write_bytes(b"fake-fcstd")


class _Object:
    """A plain non-sketch document object, e.g. a datum plane."""

    def __init__(self, type_id, name, doc):
        self.TypeId = type_id
        self.Name = name
        self.Label = name


class _App:
    def __init__(self, version=HOST_VERSION):
        major, minor, patch = (version.split(".") + ["0", "0"])[:3]
        self.Version = lambda: [major, minor, patch, "extra"]
        self.documents = {}
        self.Vector = _Vec

    def newDocument(self, name):
        doc = _Document(name, "", self)
        self.documents[name] = doc
        return doc

    def openDocument(self, path):
        key = str(path)
        if key in self.documents:
            return self.documents[key]
        doc = _Document(Path(key).stem, key, self)
        # A document opened from disk carries the datum planes an Origin created.
        for plane in ("XY_Plane", "XZ_Plane", "YZ_Plane"):
            doc.addObject("App::Plane", plane)
        self.documents[key] = doc
        return doc

    def closeDocument(self, name):
        self.documents.pop(name, None)


class _Part:
    """Stands in for FreeCAD's Part module."""

    def LineSegment(self, start, end):
        return _LineSegment(start, end)

    def Point(self, location):
        return _Point(location)

    def Circle(self, center, axis, radius):
        return _Circle(center, radius)

    def ArcOfCircle(self, circle, first, last):
        return _Circle(circle.Center, circle.Radius)


class _Sketcher:
    """Stands in for FreeCAD's Sketcher module."""

    def Constraint(self, kind, *arguments):
        return _Constraint(kind, list(arguments))


@pytest.fixture()
def host(monkeypatch):
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_write_contract"] = write_contract
    app = _App()
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "Part", _Part())
    monkeypatch.setitem(sys.modules, "Sketcher", _Sketcher())
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    return app


def _document(host, tmp_path, name="sketchdoc"):
    path = tmp_path / ("%s.FCStd" % name)
    path.write_bytes(b"document")
    return host.openDocument(str(path)), str(path)


def _sketch(host, tmp_path, name="Profile"):
    """A document with an attached, empty sketch."""
    doc, path = _document(host, tmp_path)
    body = doc.addObject("PartDesign::Body", "Body")
    sketch = body.newObject("Sketcher::SketchObject", name)
    sketch.AttachmentSupport = [(doc.getObject("XY_Plane"), "")]
    sketch.MapMode = "FlatFace"
    return doc, path, sketch


def _silence(doc):
    """Make every sketch created from here on silently drop its writes."""
    doc._silenced = True


# ---------------------------------------------------------------------------
# sketch.create
# ---------------------------------------------------------------------------


def test_create_sketch_attaches_to_the_requested_plane(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    result = freecad_driver.sketch_create({"document_path": path, "name": "Profile", "plane": "xz"})

    assert result["name"] == "Profile"
    assert result["plane"] == "xz"
    assert result["attached_plane"] == "XZ_Plane"
    assert result["body"] == "Body"
    for check in ("sketch.exists", "sketch.type_id", "sketch.support", "sketch.map_mode"):
        assert check in result["verified"], check


def test_create_sketch_defaults_to_xy(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    result = freecad_driver.sketch_create({"document_path": path, "name": "Profile"})

    assert result["plane"] == "xy"
    assert result["attached_plane"] == "XY_Plane"


def test_create_sketch_refuses_when_the_sketch_is_not_there(host, tmp_path):
    """The dropped write this read-back exists for.

    The host accepts the call and returns; the document has no sketch. A caller
    told "created" would go on to add geometry to an object that does not exist.
    """
    doc, path = _document(host, tmp_path)
    original = doc.addObject

    def addObject(type_id, name):
        obj = original(type_id, name)
        if type_id == "Sketcher::SketchObject":
            doc.Objects.remove(obj)  # the host accepted the call and kept nothing
        return obj

    doc.addObject = addObject

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_create({"document_path": path, "name": "Profile"})

    error = excinfo.value
    assert error.check == "sketch.exists"
    assert error.expected == "Profile"
    assert error.actual is None


def test_create_sketch_refuses_when_the_attachment_was_dropped(host, tmp_path):
    """The plane parameter is the point of the call; a dropped attachment ignores it.

    The sketch object exists, so no exists/type check can see this. Only reading
    the attachment back catches a sketch that quietly stayed on the global plane.
    """
    doc, path = _document(host, tmp_path)

    # Drop the attachment from the document's save hook, which runs after every
    # write the driver makes: the sketch object exists and is the right type, so
    # only reading AttachmentSupport back can see that the plane was never
    # attached.
    original_save = doc.save

    def save():
        for obj in doc.Objects:
            if obj.TypeId == "Sketcher::SketchObject":
                obj.AttachmentSupport = ()
                obj.MapMode = "Deactivated"
        original_save()

    doc.save = save

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_create({"document_path": path, "name": "Profile", "plane": "yz"})

    assert excinfo.value.check == "sketch.support"
    assert excinfo.value.expected == ["YZ_Plane"]
    assert excinfo.value.actual == []


def test_create_sketch_rejects_an_unknown_plane(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="unsupported plane 'ab'"):
        freecad_driver.sketch_create({"document_path": path, "name": "Profile", "plane": "ab"})


def test_create_sketch_rejects_a_duplicate_name(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)

    with pytest.raises(ValueError, match="object already exists: Profile"):
        freecad_driver.sketch_create({"document_path": path, "name": "Profile"})


# ---------------------------------------------------------------------------
# sketch.add_geometry
# ---------------------------------------------------------------------------


def test_add_geometry_returns_element_ids(host, tmp_path):
    _doc, path, sketch = _sketch(host, tmp_path)

    result = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "rectangle", "corner": [0, 0], "width": 80, "height": 50},
        }
    )

    # A rectangle has no primitive in Sketcher, so it expands to four segments.
    assert result["element_ids"] == [0, 1, 2, 3]
    assert result["kind"] == "rectangle"
    assert "geometry.count" in result["verified"]


def test_add_geometry_reports_circle_and_line(host, tmp_path):
    _doc, path, sketch = _sketch(host, tmp_path)

    line = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )
    circle = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "circle", "center": [5, 5], "radius": 2.5},
        }
    )

    assert line["element_ids"] == [0]
    assert circle["element_ids"] == [1]
    assert circle["geometry"][0]["radius"] == 2.5


def test_add_geometry_reports_point_and_arc(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)

    point = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "point", "at": [3, 4]},
        }
    )
    arc = freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {
                "kind": "arc",
                "center": [0, 0],
                "radius": 5,
                "start_angle": 0,
                "end_angle": 90,
            },
        }
    )

    assert point["element_ids"] == [0]
    assert arc["element_ids"] == [1]


def test_add_geometry_refuses_when_the_geometry_was_not_stored(host, tmp_path):
    _doc, path, sketch = _sketch(host, tmp_path)
    sketch._silenced = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
            }
        )

    error = excinfo.value
    assert error.check == "geometry.count"
    assert error.expected == 1
    assert error.actual == 0


def test_add_geometry_rejects_an_unknown_kind(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)

    with pytest.raises(ValueError, match="unsupported geometry kind 'hexagon'"):
        freecad_driver.sketch_add_geometry(
            {"document_path": path, "sketch_name": "Profile", "geometry": {"kind": "hexagon"}}
        )


def test_add_geometry_rejects_a_degenerate_rectangle(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)

    with pytest.raises(ValueError, match="width must be positive"):
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "geometry": {"kind": "rectangle", "corner": [0, 0], "width": 0, "height": 5},
            }
        )


def test_add_geometry_rejects_a_three_dimensional_coordinate(host, tmp_path):
    """A 2D sketch coordinate with a z component is refused, not truncated."""
    _doc, path, _sk = _sketch(host, tmp_path)

    with pytest.raises(ValueError, match="start must contain exactly two numbers"):
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "geometry": {"kind": "line", "start": [0, 0, 0], "end": [10, 0]},
            }
        )


def test_add_geometry_rejects_a_non_sketch_object(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="is not a sketch"):
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "XY_Plane",
                "geometry": {"kind": "line", "start": [0, 0], "end": [1, 0]},
            }
        )


# ---------------------------------------------------------------------------
# sketch.add_constraint
# ---------------------------------------------------------------------------


def test_every_published_constraint_kind_has_an_argument_shape():
    """A kind without a shape is a crash waiting on the first caller to use it.

    ``Sketcher.Constraint`` has no single variadic signature, and a shape that
    matches no overload segfaults the host instead of raising, so a kind added
    to the vocabulary without a shape here is not a missing optimisation -- it is
    a published tool that kills the FreeCAD process.
    """
    missing = [
        kind
        for kind, (free_cad_name, _arity) in sorted(freecad_driver._CONSTRAINT_KINDS.items())
        if free_cad_name not in freecad_driver._CONSTRAINT_SHAPES
    ]
    assert missing == [], "constraint kinds with no argument shape: %s" % missing
    orphaned = set(freecad_driver._CONSTRAINT_SHAPES) - {
        free_cad_name for free_cad_name, _arity in freecad_driver._CONSTRAINT_KINDS.values()
    }
    assert orphaned == set(), "argument shapes for unknown kinds: %s" % sorted(orphaned)


@pytest.mark.parametrize(
    "kind,targets,value,expected",
    [
        # Index-only: no point position, so ("Horizontal", 0) not ("Horizontal", 0, 0).
        ("horizontal", [{"element": 0}], None, [0]),
        ("vertical", [{"element": 0}], None, [0]),
        ("parallel", [{"element": 0}, {"element": 1}], None, [0, 1]),
        ("equal", [{"element": 0}, {"element": 1}], None, [0, 1]),
        # Point-taking: an index and a position per target.
        (
            "coincident",
            [{"element": 0, "position": "end"}, {"element": 1, "position": "start"}],
            None,
            [0, 2, 1, 1],
        ),
        # PointOnObject takes three arguments: the first target's index and
        # position, then the second target's index.
        ("point_on_object", [{"element": 0, "position": "end"}, {"element": 1}], None, [0, 2, 1]),
        # Dimensional on a single element: index then value, no position.
        ("distance", [{"element": 0, "position": "end"}], 10.0, [0, 10.0]),
        ("radius", [{"element": 0, "position": "end"}], 5.0, [0, 5.0]),
        ("diameter", [{"element": 0, "position": "end"}], 5.0, [0, 5.0]),
        # Dimensional across two point references: positions included.
        (
            "distance_x",
            [{"element": 0, "position": "end"}, {"element": 1, "position": "start"}],
            80.0,
            [0, 2, 1, 1, 80.0],
        ),
        ("angle", [{"element": 0}, {"element": 1}], 90.0, [0, 0, 1, 0, 90.0]),
    ],
)
def test_constraint_arguments_use_the_overload_the_host_accepts(kind, targets, value, expected):
    """The argument list must match an overload FreeCAD actually has.

    ``Sketcher.Constraint`` rejects no shape cleanly: a mismatch crashes the host
    process, so a wrong shape here is a segfault and an empty result file rather
    than a Python error. These are the shapes enumerated on real FreeCAD 1.0.2
    and 1.1.4, where Radius, Diameter and PointOnObject segfault given an extra
    point position.
    """
    tool = "sketch.add_constraint"
    name, _arity = freecad_driver._CONSTRAINT_KINDS[kind]
    arguments = freecad_driver._constraint_arguments(name, targets, value, tool)
    assert arguments == expected


def test_a_single_target_distance_addresses_the_element_not_a_point_on_itself(host, tmp_path):
    """A Distance on one element means that element's length.

    Built with a point position it becomes the distance from the element's first
    point to itself, which the host accepts and reports as zero length rather
    than rejecting: the driver would return success with the wrong constraint.
    """
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )
    result = freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "type": "distance",
            "targets": [{"element": 0}],
            "value": 10.0,
        }
    )
    assert result["constraint"]["value"] == 10.0
    assert result["constraint"]["second"] == -2000, (
        "a single-target Distance must address the element, not a point on itself"
    )


def test_add_constraint_reports_the_dof_it_removed(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    result = freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "type": "horizontal",
            "targets": [{"element": 0}],
        }
    )

    assert result["constraint_id"] == 0
    assert result["type"] == "horizontal"
    assert result["dof_delta"] == 1
    assert "constraint.count" in result["verified"]


def test_add_constraint_stores_a_dimensional_value(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    result = freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "type": "distance",
            "targets": [{"element": 0}],
            "value": 80,
        }
    )

    assert result["constraint"]["value"] == 80
    assert "constraint.value" in result["verified"]


def test_add_constraint_rewrites_concentric_as_a_coincident_centre(host, tmp_path):
    """Concentric is the name callers use; FreeCAD spells it Coincident on centres."""
    _doc, path, _sk = _sketch(host, tmp_path)
    for center in ([0, 0], [10, 0]):
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "geometry": {"kind": "circle", "center": center, "radius": 5},
            }
        )

    result = freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "type": "concentric",
            "targets": [{"element": 0, "position": "mid"}, {"element": 1, "position": "mid"}],
        }
    )

    assert result["constraint"]["type"] == "Coincident"
    assert result["constraint"]["first_pos"] == 3
    assert result["constraint"]["second_pos"] == 3


def test_add_constraint_refuses_a_reference_to_a_missing_element(host, tmp_path):
    """A dangling reference is an error, never something the solver ignores."""
    _doc, path, _sk = _sketch(host, tmp_path)

    with pytest.raises(ValueError, match="element 3 does not exist in this sketch"):
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "type": "horizontal",
                "targets": [{"element": 3}],
            }
        )


def test_add_constraint_refuses_a_reference_as_a_string(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    with pytest.raises(ValueError, match="element index must be an integer"):
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "type": "horizontal",
                "targets": [{"element": "0"}],
            }
        )


def test_add_constraint_refuses_when_the_constraint_was_not_stored(host, tmp_path):
    _doc, path, sketch = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )
    sketch._silenced = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "type": "horizontal",
                "targets": [{"element": 0}],
            }
        )

    error = excinfo.value
    assert error.check == "constraint.count"
    assert error.expected == 1
    assert error.actual == 0


def test_add_constraint_requires_a_value_for_a_dimensional_constraint(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    with pytest.raises(ValueError, match="constraint distance requires a 'value'"):
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "type": "distance",
                "targets": [{"element": 0}],
            }
        )


def test_add_constraint_refuses_a_value_on_a_geometric_constraint(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    with pytest.raises(ValueError, match="does not take a 'value'"):
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "type": "horizontal",
                "targets": [{"element": 0}],
                "value": 10,
            }
        )


def test_add_constraint_rejects_the_wrong_number_of_targets(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    with pytest.raises(ValueError, match="takes exactly 1 target"):
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "type": "horizontal",
                "targets": [{"element": 0}, {"element": 0}],
            }
        )


def test_add_constraint_rejects_an_unknown_position(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    with pytest.raises(ValueError, match="unknown point position 'corner'"):
        freecad_driver.sketch_add_constraint(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "type": "horizontal",
                "targets": [{"element": 0, "position": "corner"}],
            }
        )


# ---------------------------------------------------------------------------
# The under-constrained gate
# ---------------------------------------------------------------------------


def _gate(host, tmp_path, allow=False):
    _doc, path, sketch = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )
    return freecad_driver._require_fully_constrained(
        sketch, "sketch.feature", HOST_VERSION, {"sketch_name": "Profile"}, {}, allow
    )


def test_the_gate_refuses_an_underconstrained_sketch(host, tmp_path):
    with pytest.raises(write_contract.UnderconstrainedSketchError) as excinfo:
        _gate(host, tmp_path)

    error = excinfo.value
    assert error.check == write_contract.SKETCH_UNDERCONSTRAINED
    assert error.tool == "sketch.feature"
    assert error.expected == "dof == 0"
    assert error.actual > 0
    assert error.host_version == HOST_VERSION
    assert "degrees of freedom" in str(error)


def test_the_gate_passes_a_fully_constrained_sketch(host, tmp_path):
    _doc, path, sketch = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )
    freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "type": "horizontal",
            "targets": [{"element": 0}],
        }
    )
    freecad_driver.sketch_add_constraint(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "type": "distance",
            "targets": [{"element": 0}],
            "value": 10,
        }
    )

    assert freecad_driver._dof(sketch) == 0
    dof = freecad_driver._require_fully_constrained(sketch, "sketch.feature", HOST_VERSION, {}, {})
    assert dof == 0


def test_the_gate_lets_incremental_sketch_building_through(host, tmp_path):
    """The escape valve is what makes the gate a gate and not a wall.

    A sketch is built one element at a time and every intermediate state has
    degrees of freedom left, so the geometry and constraint calls opt out.
    Without this, the first call after create_sketch would always fail.
    """
    assert _gate(host, tmp_path, allow=True) > 0


def test_the_underconstrained_payload_survives_serialisation():
    error = write_contract.UnderconstrainedSketchError(
        tool="sketch.feature",
        expected="dof == 0",
        actual=3,
        host_version="1.0.2",
        host_matrix={"status": "supported", "range_id": "1.0.x"},
        params={"sketch_name": "Profile"},
    )

    revived = write_contract.UnderconstrainedSketchError.from_payload(
        json.loads(json.dumps(error.payload))
    )

    assert revived.payload == error.payload
    assert str(revived) == str(error)
    assert "3 degree(s) of freedom" in str(revived)


def test_dof_is_read_after_a_solve(host, tmp_path):
    """A stale DOF would report the state before the call, not after it.

    ``DoF`` reflects the last solver run, so ``_dof`` must solve first: reading
    the property without solving would answer for the state before the write.
    """
    _doc, path, sketch = _sketch(host, tmp_path)
    sketch._solved = False  # as if nothing had been solved since the last write

    freecad_driver._dof(sketch)

    assert sketch._solved is True


def test_a_host_without_dof_is_refused_loudly(host, tmp_path):
    """An unmeasurable sketch must never be reported as constrained.

    Without ``DoF`` there is no way to tell a finished profile from a floating
    one, so measuring it is the only safe answer -- the alternative is reporting
    every sketch as fully constrained.
    """
    _doc, path, sketch = _sketch(host, tmp_path)

    class _NoDofSketch:
        """A host whose SketchObject lost the DoF property."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "DoF":
                raise AttributeError(name)
            return getattr(self._inner, name)

    with pytest.raises(freecad_driver.IncompatibleHostError, match="DoF"):
        freecad_driver._dof(_NoDofSketch(sketch))


# ---------------------------------------------------------------------------
# sketch.get_info
# ---------------------------------------------------------------------------


def test_get_info_reports_dof_and_constraint_status(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )

    info = freecad_driver.sketch_get_info({"document_path": path, "sketch_name": "Profile"})

    assert info["geometry_count"] == 1
    assert info["constraint_count"] == 0
    assert info["dof"] > 0
    assert info["fully_constrained"] is False


def test_get_info_reports_a_fully_constrained_sketch(host, tmp_path):
    _doc, path, _sk = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )
    for constraint in (
        {"type": "horizontal", "targets": [{"element": 0}]},
        {"type": "distance", "targets": [{"element": 0}], "value": 10},
    ):
        freecad_driver.sketch_add_constraint(
            {"document_path": path, "sketch_name": "Profile", **constraint}
        )

    info = freecad_driver.sketch_get_info({"document_path": path, "sketch_name": "Profile"})

    assert info["dof"] == 0
    assert info["fully_constrained"] is True
    assert info["constraint_count"] == 2


def test_get_info_is_read_only(host, tmp_path):
    """An agent must be able to ask 'is this done?' without changing the answer."""
    _doc, path, sketch = _sketch(host, tmp_path)
    freecad_driver.sketch_add_geometry(
        {
            "document_path": path,
            "sketch_name": "Profile",
            "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
        }
    )
    before = (len(sketch.Geometry), len(sketch.Constraints))

    freecad_driver.sketch_get_info({"document_path": path, "sketch_name": "Profile"})

    assert (len(sketch.Geometry), len(sketch.Constraints)) == before
    assert "sketch.get_info" in write_contract.READ_ONLY_TOOLS


def test_get_info_rejects_a_non_sketch(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="is not a sketch"):
        freecad_driver.sketch_get_info({"document_path": path, "sketch_name": "XY_Plane"})


# ---------------------------------------------------------------------------
# The refusal must reach the caller with its structure intact
# ---------------------------------------------------------------------------


def test_the_driver_forwards_the_underconstrained_payload(host, tmp_path, monkeypatch):
    """A refusal travels under its own key, so a caller can branch on it.

    The gate is routed through a sketch request so the payload crosses the real
    process boundary in ``main()``: that is where the ``underconstrained`` key is
    chosen over ``write_verification``, which is the part worth testing.
    """
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    doc, path = _document(host, tmp_path)
    body = doc.addObject("PartDesign::Body", "Body")
    sketch = body.newObject("Sketcher::SketchObject", "Profile")
    sketch.Geometry = [_LineSegment(_Vec(0, 0, 0), _Vec(10, 0, 0))]
    request.write_text(
        json.dumps(
            {
                "method": "sketch.add_geometry",
                "params": {
                    "document_path": str(path),
                    "sketch_name": "Profile",
                    "geometry": {"kind": "point", "at": [1, 2]},
                },
            }
        ),
        encoding="utf-8",
    )
    # A point is the one geometry that cannot be driven to zero freedom with the
    # constraints this vocabulary exposes, so the gate genuinely has something to
    # refuse. monkeypatch restores argv; assigning it here would leak.
    real_require = freecad_driver._require_fully_constrained

    def strict(sketch, tool, version, params, host_matrix, allow_underconstrained=False):
        return real_require(
            sketch, tool, version, params, host_matrix, allow_underconstrained=False
        )

    monkeypatch.setattr(freecad_driver, "_require_fully_constrained", strict)
    monkeypatch.setattr(sys, "argv", ["driver", "--pass", str(request), str(result)])

    freecad_driver.main()

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    error = payload["error"]
    assert "underconstrained" in error, error
    assert "write_verification" not in error, error
    assert error["underconstrained"]["check"] == write_contract.SKETCH_UNDERCONSTRAINED


def test_a_silenced_host_never_reports_a_sketch_as_constrained(host, tmp_path):
    """The failure the gate exists to prevent, seen end to end.

    With writes dropped, the sketch reports zero elements and therefore zero DOF,
    which is what a caller would read as "fully constrained". The geometry
    read-back is what catches it: it refuses before the DOF number is trusted.
    """
    doc, path = _document(host, tmp_path)
    body = doc.addObject("PartDesign::Body", "Body")
    sketch = body.newObject("Sketcher::SketchObject", "Profile")
    sketch._silenced = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.sketch_add_geometry(
            {
                "document_path": path,
                "sketch_name": "Profile",
                "geometry": {"kind": "line", "start": [0, 0], "end": [10, 0]},
            }
        )

    assert excinfo.value.check == "geometry.count"
