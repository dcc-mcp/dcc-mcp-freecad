"""The PartDesign feature driver against a fake FreeCAD host.

This is the lane where the volume postcondition is proved to *fire*. The fake
stores what it is told, so a feature the host silently drops is observably
missing, and the volume read-back is what catches it. The real-host lane in
``tests/test_partdesign_real.py`` proves the same checks do not fire on real
geometry on FreeCAD 1.0.2 and 1.1.4.

The bug this file is built around is upstream FreeCAD issue #99: ``pocket``
reports success and a Valid state while removing no material. Every test here
that ends in ``_refused`` drives that exact shape -- a feature that ran, was
saved, and changed nothing -- and asserts the adapter reports an error instead
of a success carrying a zero.
"""

from __future__ import annotations

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
PAD_ID = "PartDesign::Pad"
POCKET_ID = "PartDesign::Pocket"
REVOLUTION_ID = "PartDesign::Revolution"
GROOVE_ID = "PartDesign::Groove"
LOFT_ID = "PartDesign::AdditiveLoft"
SWEEP_ID = "PartDesign::AdditivePipe"
HOLE_ID = "PartDesign::Hole"

PROFILE_AREA = 40.0 * 10.0
# The profile's centroid distance from the revolution axis. The volume a
# revolution sweeps depends on it (Pappus), so the fake models it explicitly
# rather than folding it into a constant.
PROFILE_CENTROID_X = 25.0


# ---------------------------------------------------------------------------
# A FreeCAD that can drop a feature without saying so
# ---------------------------------------------------------------------------


class _Centre:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class _Shape:
    def __init__(self, volume=0.0, valid=True, solids=1, area=0.0, centre=None):
        self.Volume = float(volume)
        self._valid = valid
        self.Solids = [object()] * solids
        self.ShapeType = "Solid" if solids else "Shell"
        self.Area = float(area)
        self.CenterOfMass = centre or _Centre()
        self.isNull = lambda: False

    def isValid(self):
        return self._valid


class _Object:
    def __init__(self, type_id, name):
        self.TypeId = type_id
        self.Name = name
        self.Label = name
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
        self.Document = doc
        self.Tip = None

    def newObject(self, type_id, name):
        return self._doc._add(type_id, name, self)


class _Sketch(_Object):
    """A sketch whose profile area is whatever the test says the host measured.

    Real FreeCAD reports a sketch's enclosed area on ``Shape.Area``; the driver
    reads it there, so the fake exposes it there and keeps it in step with the
    ``area`` attribute a test sets.
    """

    def __init__(self, name):
        super().__init__(SKETCH_ID, name)
        self.Geometry = [object()]
        self.Constraints = [object()]
        self.ExternalGeometry = []
        self.DoF = 0
        self.MapMode = "FlatFace"
        self._area = PROFILE_AREA
        self._centre_x = PROFILE_CENTROID_X
        self.Shape = _Shape(volume=0.0, area=PROFILE_AREA, centre=_Centre(x=PROFILE_CENTROID_X))

    @property
    def area(self):
        return self._area

    @area.setter
    def area(self, value):
        self._area = value
        self.Shape.Area = float(value)

    @property
    def centre_x(self):
        return self._centre_x

    @centre_x.setter
    def centre_x(self, value):
        self._centre_x = value
        self.Shape.CenterOfMass = _Centre(x=value)

    def solve(self):
        return 0


class _Feature(_Object):
    """A PartDesign feature that stores the properties it is given.

    ``drop`` models the failure the read-back exists for: the host accepts every
    write, recomputes cleanly, saves, and then produces a feature that changed
    nothing. ``side_property`` pins which reversal spelling this host offers.
    """

    def __init__(self, type_id, name, doc):
        super().__init__(type_id, name)
        self._doc = doc
        self.Profile = None
        self.Length = None
        self.Length2 = None
        self.Angle = None
        self.Axis = None
        self.Sections = None
        self.Spine = None
        self.Diameter = None
        self.Depth = None
        self.HoleCutType = None
        self.Reversed = False
        self.drop = False
        # Which generation of the reversal property this host exposes.
        self.side_property = doc.side_property

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)

    def _has(self, name):
        return hasattr(self, name)


def _install_side_property(feature, doc):
    """Give a feature whichever reversal spelling this host offers."""
    name = doc.side_property
    if name is None:
        return
    if name == "SideType":
        feature.SideType = "One side"
    else:
        setattr(feature, name, False)


class _Document:
    def __init__(self, name, path="", app=None):
        self.Name = name
        self.Label = name
        self.FileName = path
        self.Objects = []
        self._app = app
        # Which reversal spelling this host exposes. ``None`` models a host that
        # moved the property somewhere the adapter has never seen.
        self.side_property = "SideType"
        # When set, a created feature changes nothing: the upstream #99 shape.
        self.drop_features = False
        self.feature_volume_override = None
        # The volume each body has accumulated, keyed by body name. Kept across
        # recomputes so a second pass replays history instead of stacking it.
        self._running = {}
        # Which features have already been computed, and the volume each
        # produced, so a repeat recompute replays rather than re-applies.
        self._computed = set()
        self._result = {}

    def addObject(self, type_id, name):
        return self._add(type_id, name, None)

    def _add(self, type_id, name, parent):
        if type_id == BODY_ID:
            obj = _Body(name, self)
        elif type_id == PLANE_ID:
            obj = _Plane(name)
        elif type_id == SKETCH_ID:
            obj = _Sketch(name)
        else:
            obj = _Feature(type_id, name, self)
            _install_side_property(obj, self)
        if parent is not None:
            obj.InList.append(parent)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def recompute(self):
        # Real FreeCAD's recompute is idempotent: recomputing an unchanged
        # document leaves the geometry where it was. The driver recomputes twice
        # (once after configuring, once inside save), so each feature's
        # contribution is computed exactly once and replayed after that --
        # otherwise every feature would be applied twice.
        for obj in self.Objects:
            if not isinstance(obj, _Feature):
                continue
            body = obj.InList[0] if obj.InList else None
            body_name = body.Name if body is not None else None
            if obj.Name in self._computed:
                obj.Shape = _Shape(volume=self._result[obj.Name], solids=1)
            elif obj.drop or self.drop_features:
                # The upstream #99 shape: the feature exists, is a valid solid,
                # and passes the body through unchanged. It did not produce an
                # empty shape -- it just did nothing -- which is exactly why
                # only a volume comparison catches it.
                obj.Shape = _Shape(volume=self._running.get(body_name, 0.0), solids=1)
                self._computed.add(obj.Name)
                self._result[obj.Name] = obj.Shape.Volume
            else:
                before = self._running.get(body_name, 0.0)
                volume = self._feature_volume(obj, before)
                obj.Shape = _Shape(volume=volume)
                self._running[body_name] = volume
                self._computed.add(obj.Name)
                self._result[obj.Name] = volume
            if body is not None:
                body.Tip = obj
                body.Shape = obj.Shape

    def _feature_volume(self, obj, before=0.0):
        """The volume the feature contributes, from the properties it was set to.

        The fake models the geometry properly enough for the read-back to be
        meaningful: a pad's volume is its profile area times its length, and the
        body's accumulated volume is what the tool measures.
        """
        if self.feature_volume_override is not None:
            return self.feature_volume_override
        area = float(getattr(getattr(obj, "Profile", None), "area", PROFILE_AREA) or 0.0)
        if obj.TypeId in (PAD_ID, POCKET_ID):
            length = float(obj.Length or 0.0)
            if obj.TypeId == PAD_ID:
                return before + area * length
            return max(before - area * length, 0.0)
        if obj.TypeId in (REVOLUTION_ID, GROOVE_ID):
            # Pappus: the profile sweeps its area along the circular path its
            # centroid travels, so the radius is the centroid distance.
            angle = float(obj.Angle or 0.0)
            profile = getattr(obj, "Profile", None)
            radius = float(getattr(profile, "centre_x", PROFILE_CENTROID_X) or 0.0)
            swept = area * 2.0 * math.pi * radius * (angle / 360.0)
            return before + swept if obj.TypeId == REVOLUTION_ID else max(before - swept, 0.0)
        if obj.TypeId in (LOFT_ID, SWEEP_ID):
            # A loft or sweep spans the distance between its sections, so the
            # driver records that span on ``Length`` for the volume bound.
            length = float(getattr(obj, "Length", None) or 0.0)
            return before + area * length
        if obj.TypeId == HOLE_ID:
            radius = float(obj.Diameter or 0.0) / 2.0
            return max(before - math.pi * radius * radius * float(obj.Depth or 0.0), 0.0)
        return before

    def save(self):
        pass

    def saveAs(self, path):
        self.FileName = path
        Path(path).write_bytes(b"fake-fcstd")


class _App:
    def __init__(self, version=HOST_VERSION):
        major, minor, patch = (version.split(".") + ["0", "0"])[:3]
        self.Version = lambda: [major, minor, patch, "extra"]
        self._documents = {}

    def openDocument(self, path):
        doc = self._documents.get(path)
        if doc is None:
            doc = _Document(Path(path).stem, path, self)
            self._documents[path] = doc
        return doc

    def closeDocument(self, name):
        # The driver closes the document after every call, so a test that runs
        # several features in sequence reopens the same path. Real FreeCAD reads
        # the geometry back from disk; the fake keeps the in-memory document,
        # which is what makes a follow-up feature see the previous one's body.
        return


class FakePart(types.ModuleType):
    def __init__(self):
        super().__init__("Part")


@pytest.fixture()
def host(monkeypatch):
    """Install a fake FreeCAD and return the harness."""
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_write_contract"] = write_contract
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_sketch_rules"] = sketch_rules
    app = _App()
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "Part", FakePart())
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    return types.SimpleNamespace(app=app)


def _document(host, tmp_path, name="model"):
    path = tmp_path / ("%s.FCStd" % name)
    path.write_bytes(b"document")
    doc = host.app.openDocument(str(path))
    body = doc.addObject(BODY_ID, "Body")
    sketch = body.newObject(SKETCH_ID, "ProfileSketch")
    sketch.area = PROFILE_AREA
    return doc, str(path)


def _pad(host, path, **extra):
    params = {
        "document_path": path,
        "sketch_name": "ProfileSketch",
        "result_name": "Pad",
        "length": 10.0,
    }
    params.update(extra)
    return freecad_driver.partdesign_pad(params)


# ---------------------------------------------------------------------------
# A pad that adds material
# ---------------------------------------------------------------------------


def test_pad_adds_the_profile_area_times_the_length(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    result = _pad(host, path)

    expected = PROFILE_AREA * 10.0
    assert result["volume"]["before"] == 0.0
    assert result["volume"]["delta"] == pytest.approx(expected)
    assert result["volume"]["direction"] == "add"
    assert result["feature"]["type_id"] == PAD_ID
    assert result["feature"]["profile"] == "ProfileSketch"
    # ``one_side`` is the default and writes no reversal property at all.
    assert result["feature"]["side_property"] is None


def test_pad_reports_the_checks_that_ran(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    result = _pad(host, path)

    for check in (
        "feature.exists",
        "feature.type_id",
        "feature.profile",
        "feature.in_body",
        "feature.volume_delta.direction",
        "feature.volume_delta.magnitude",
    ):
        assert check in result["verified"]


# ---------------------------------------------------------------------------
# The upstream #99 shape: reported success, nothing removed
# ---------------------------------------------------------------------------


def test_a_pad_that_added_nothing_is_refused(host, tmp_path):
    """A pad whose host dropped the change is an error, not a success."""
    doc, path = _document(host, tmp_path)
    doc.drop_features = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        _pad(host, path)

    assert excinfo.value.check == "feature.volume_delta.direction"
    # Both numbers are reported, so the caller sees the gap instead of guessing.
    assert excinfo.value.expected is not None
    assert excinfo.value.actual == 0.0


def test_a_pocket_that_removed_nothing_is_refused(host, tmp_path):
    """Upstream #99: pocket reports success while removing no material."""
    doc, path = _document(host, tmp_path)
    doc.drop_features = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.partdesign_pocket(
            {
                "document_path": path,
                "sketch_name": "ProfileSketch",
                "result_name": "Pocket",
                "length": 5.0,
            }
        )

    assert excinfo.value.check == "feature.volume_delta.direction"
    assert excinfo.value.actual == 0.0


def test_a_pocket_that_increased_the_volume_is_refused(host, tmp_path):
    """A pocket that grew the body did the opposite of what was asked."""
    doc, path = _document(host, tmp_path)
    # The host "removes" material by adding to the body.
    doc.feature_volume_override = 5000.0

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.partdesign_pocket(
            {
                "document_path": path,
                "sketch_name": "ProfileSketch",
                "result_name": "Pocket",
                "length": 5.0,
            }
        )

    assert excinfo.value.check == "feature.volume_delta.direction"


def test_a_pad_that_delivered_far_too_little_is_refused(host, tmp_path):
    """A clipped feature passes the direction check but not the magnitude one."""
    doc, path = _document(host, tmp_path)
    # One hundredth of the requested volume: direction is right, size is not.
    doc.feature_volume_override = PROFILE_AREA * 10.0 * 0.01

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        _pad(host, path)

    assert excinfo.value.check == "feature.volume_delta.magnitude"


# ---------------------------------------------------------------------------
# Degenerate profiles and extents
# ---------------------------------------------------------------------------


def test_a_profile_that_encloses_no_area_is_refused(host, tmp_path):
    """An open or self-intersecting profile must not become an empty shell."""
    doc, path = _document(host, tmp_path)
    doc.getObject("ProfileSketch").area = 0.0

    with pytest.raises(ValueError) as excinfo:
        _pad(host, path)

    assert getattr(excinfo.value, "code", None) == "E_PROFILE_DEGENERATE"


def test_a_non_finite_profile_area_is_refused(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.getObject("ProfileSketch").area = float("nan")

    with pytest.raises(ValueError) as excinfo:
        _pad(host, path)

    assert getattr(excinfo.value, "code", None) == "E_PROFILE_DEGENERATE"


def test_a_zero_length_extent_is_refused(host, tmp_path):
    """A zero extent is refused before anything is written."""
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        _pad(host, path, length=0.0)

    assert getattr(excinfo.value, "code", None) in (
        "E_EXTENT_DEGENERATE",
        "E_LENGTH_INVALID",
    )


def test_a_negative_length_extent_is_refused(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError):
        _pad(host, path, length=-10.0)


# ---------------------------------------------------------------------------
# Under-constrained sketches are refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dof", [1, 2, 7])
def test_an_under_constrained_profile_is_refused(host, tmp_path, dof):
    doc, path = _document(host, tmp_path)
    doc.getObject("ProfileSketch").DoF = dof

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        _pad(host, path)

    assert excinfo.value.code == sketch_rules.ERROR_UNDERCONSTRAINED


def test_an_over_constrained_profile_is_refused(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.getObject("ProfileSketch").DoF = -3

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        _pad(host, path)

    assert excinfo.value.code == sketch_rules.ERROR_OVERCONSTRAINED


def test_a_sketch_with_no_geometry_is_refused(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.getObject("ProfileSketch").Geometry = []

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        _pad(host, path)

    assert excinfo.value.code == sketch_rules.ERROR_UNDERCONSTRAINED


def test_a_sketch_outside_a_body_is_refused(host, tmp_path):
    path = tmp_path / "loose.FCStd"
    path.write_bytes(b"document")
    doc = host.app.openDocument(str(path))
    doc.addObject(SKETCH_ID, "LooseSketch")

    with pytest.raises(ValueError) as excinfo:
        freecad_driver.partdesign_pad(
            {
                "document_path": str(path),
                "sketch_name": "LooseSketch",
                "result_name": "Pad",
                "length": 10.0,
            }
        )

    assert getattr(excinfo.value, "code", None) == "E_SKETCH_NOT_IN_BODY"


# ---------------------------------------------------------------------------
# The side-type ladder is probed, never assumed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling,expected_value",
    [("SideType", "Two sides"), ("Midplane", True), ("Symmetric", True)],
)
def test_the_side_type_uses_whichever_spelling_the_host_offers(
    host, tmp_path, spelling, expected_value
):
    """Every generation of the reversal property is driven correctly."""
    doc, path = _document(host, tmp_path)
    doc.side_property = spelling

    result = _pad(host, path, side_type="two_sides")

    assert result["feature"]["side_property"] == spelling
    feature = doc.getObject("Pad")
    assert getattr(feature, spelling) == expected_value


def test_one_side_writes_no_reversal_property(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.side_property = "SideType"

    result = _pad(host, path, side_type="one_side")

    assert result["feature"]["side_property"] is None
    assert doc.getObject("Pad").SideType == "One side"


def test_a_host_exposing_no_reversal_property_is_refused(host, tmp_path):
    """Refusing beats writing to a name that silently does nothing."""
    doc, path = _document(host, tmp_path)
    doc.side_property = None

    with pytest.raises(freecad_driver.IncompatibleHostError):
        _pad(host, path, side_type="symmetric")


def test_an_unknown_side_type_is_refused(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        _pad(host, path, side_type="sideways")

    assert getattr(excinfo.value, "code", None) == "E_SIDE_TYPE_INVALID"


# ---------------------------------------------------------------------------
# Revolution and Groove carry no side type
# ---------------------------------------------------------------------------


def test_revolution_adds_the_swept_volume(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    result = freecad_driver.partdesign_revolution(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "Revolve",
            "angle_degrees": 360.0,
        }
    )

    expected = PROFILE_AREA * 2.0 * math.pi * PROFILE_CENTROID_X
    assert result["volume"]["delta"] == pytest.approx(expected)
    assert result["feature"]["type_id"] == REVOLUTION_ID


def test_a_partial_revolution_sweeps_a_proportional_volume(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    result = freecad_driver.partdesign_revolution(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "HalfRevolve",
            "angle_degrees": 180.0,
        }
    )

    expected = PROFILE_AREA * 2.0 * math.pi * PROFILE_CENTROID_X * 0.5
    assert result["volume"]["delta"] == pytest.approx(expected)


def test_groove_removes_the_swept_volume(host, tmp_path):
    doc, path = _document(host, tmp_path)
    # A full turn about an axis 25 mm out sweeps far more than a short pad
    # holds, so the body is given enough material for the groove to remove the
    # whole swept volume rather than being clipped by an empty body.
    swept = PROFILE_AREA * 2.0 * math.pi * PROFILE_CENTROID_X
    freecad_driver.partdesign_pad(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "BasePad",
            "length": swept / PROFILE_AREA,
        }
    )

    result = freecad_driver.partdesign_groove(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "Groove",
            "angle_degrees": 360.0,
        }
    )

    assert result["volume"]["delta"] == pytest.approx(-swept)
    assert result["volume"]["direction"] == "remove"


@pytest.mark.parametrize("angle", [0.0, -90.0, 400.0])
def test_an_out_of_range_angle_is_refused(host, tmp_path, angle):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        freecad_driver.partdesign_revolution(
            {
                "document_path": path,
                "sketch_name": "ProfileSketch",
                "result_name": "Revolve",
                "angle_degrees": angle,
            }
        )

    assert getattr(excinfo.value, "code", None) == "E_ANGLE_INVALID"


def test_revolution_refuses_an_under_constrained_profile(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.getObject("ProfileSketch").DoF = 4

    with pytest.raises(sketch_rules.SketchStateError) as excinfo:
        freecad_driver.partdesign_revolution(
            {
                "document_path": path,
                "sketch_name": "ProfileSketch",
                "result_name": "Revolve",
                "angle_degrees": 360.0,
            }
        )

    assert excinfo.value.code == sketch_rules.ERROR_UNDERCONSTRAINED


# ---------------------------------------------------------------------------
# Loft, sweep and hole
# ---------------------------------------------------------------------------


def test_loft_adds_volume(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.getObject("Body")
    body.newObject(SKETCH_ID, "TopSketch")

    result = freecad_driver.partdesign_loft(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "sketch_names": ["ProfileSketch", "TopSketch"],
            "result_name": "Loft",
            "length": 40.0,
        }
    )

    assert result["volume"]["delta"] == pytest.approx(PROFILE_AREA * 40.0)
    assert doc.getObject("Loft").Sections == ["ProfileSketch", "TopSketch"]


def test_a_loft_needs_at_least_two_sections(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        freecad_driver.partdesign_loft(
            {
                "document_path": path,
                "sketch_name": "ProfileSketch",
                "sketch_names": ["ProfileSketch"],
                "result_name": "Loft",
                "length": 40.0,
            }
        )

    assert getattr(excinfo.value, "code", None) == "E_SECTIONS_REQUIRED"


def test_sweep_adds_volume(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.getObject("Body")
    body.newObject(SKETCH_ID, "PathSketch")

    result = freecad_driver.partdesign_sweep(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "path_sketch_name": "PathSketch",
            "result_name": "Sweep",
            "length": 60.0,
        }
    )

    assert result["volume"]["delta"] == pytest.approx(PROFILE_AREA * 60.0)
    assert doc.getObject("Sweep").Spine == "PathSketch"


def test_a_hole_removes_the_cylinder_volume(host, tmp_path):
    doc, path = _document(host, tmp_path)
    freecad_driver.partdesign_pad(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "BasePad",
            "length": 40.0,
        }
    )

    result = freecad_driver.partdesign_hole(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "Hole",
            "diameter": 8.0,
            "depth": 12.0,
        }
    )

    expected = math.pi * 4.0 * 4.0 * 12.0
    assert result["volume"]["delta"] == pytest.approx(-expected)
    assert result["volume"]["direction"] == "remove"


def test_a_hole_that_drilled_nothing_is_refused(host, tmp_path):
    doc, path = _document(host, tmp_path)
    freecad_driver.partdesign_pad(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "BasePad",
            "length": 40.0,
        }
    )
    doc.drop_features = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.partdesign_hole(
            {
                "document_path": path,
                "sketch_name": "ProfileSketch",
                "result_name": "Hole",
                "diameter": 8.0,
                "depth": 12.0,
            }
        )

    assert excinfo.value.check == "feature.volume_delta.direction"


def test_an_unknown_hole_type_is_refused(host, tmp_path):
    doc, path = _document(host, tmp_path)
    freecad_driver.partdesign_pad(
        {
            "document_path": path,
            "sketch_name": "ProfileSketch",
            "result_name": "BasePad",
            "length": 40.0,
        }
    )

    with pytest.raises(ValueError) as excinfo:
        freecad_driver.partdesign_hole(
            {
                "document_path": path,
                "sketch_name": "ProfileSketch",
                "result_name": "Hole",
                "diameter": 8.0,
                "depth": 12.0,
                "hole_type": "hexagonal",
            }
        )

    assert getattr(excinfo.value, "code", None) == "E_HOLE_TYPE_INVALID"


# ---------------------------------------------------------------------------
# A feature's own result must be a solid
# ---------------------------------------------------------------------------


def test_a_feature_that_produced_no_solid_is_refused(host, tmp_path):
    """A feature that yields a shell instead of a solid is refused outright.

    This is the one case the volume comparison cannot own: a shell has no
    volume to compare, so it is caught before the comparison is reached.
    """
    doc, path = _document(host, tmp_path)
    original = doc.recompute
    doc.recompute = lambda: None  # the feature keeps its null Shape
    try:
        with pytest.raises(ValueError) as excinfo:
            _pad(host, path)
    finally:
        doc.recompute = original

    assert getattr(excinfo.value, "code", None) == "E_FEATURE_EMPTY"


def test_a_duplicate_result_name_is_refused(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Feature", "Pad")

    with pytest.raises(ValueError) as excinfo:
        _pad(host, path)

    assert getattr(excinfo.value, "code", None) == "E_RESULT_EXISTS"
