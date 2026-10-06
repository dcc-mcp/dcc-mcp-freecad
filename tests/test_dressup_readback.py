"""The dress-up read-backs fire when a write does not stick.

The real-host lane in ``tests/test_dressup_patterns.py`` proves these checks do
not fire on real geometry. This file proves the opposite half: when the host
silently drops the change, the tool refuses instead of handing back a
plausible-looking payload. That is the whole reason the contract exists, and a
check that only ever passes is a check nobody can trust.

The fake FreeCAD below is deliberately synthetic. It is not a cube with correct
topology; it is the smallest set of faces and edges that gives
``_edge_size_limit`` something to measure, so the feasibility bound can be
exercised without a kernel.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import freecad_driver, write_contract  # noqa: E402

HOST_VERSION = "1.1.4"

# The synthetic solid's adjacent-face extent, which the feasibility bound
# measures. It is 10 because the faces below span 10 units from the edge.
FACE_EXTENT = 10.0


# ---------------------------------------------------------------------------
# The smallest FreeCAD that can be told to drop a write
# ---------------------------------------------------------------------------


class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __sub__(self, other):
        return _Vector(self.x - other.x, self.y - other.y, self.z - other.z)

    def __add__(self, other):
        return _Vector(self.x + other.x, self.y + other.y, self.z + other.z)

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other):
        return _Vector(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    @property
    def Length(self):
        return (self.dot(self)) ** 0.5

    def normalize(self):
        length = self.Length or 1.0
        self.x, self.y, self.z = self.x / length, self.y / length, self.z / length

    def negative(self):
        return _Vector(-self.x, -self.y, -self.z)


class _BoundBox:
    def __init__(self, low=(0.0, 0.0, 0.0), high=(1.0, 1.0, 1.0)):
        self.XMin, self.YMin, self.ZMin = (float(item) for item in low)
        self.XMax, self.YMax, self.ZMax = (float(item) for item in high)

    @property
    def XLength(self):
        return self.XMax - self.XMin

    @property
    def YLength(self):
        return self.YMax - self.YMin

    @property
    def ZLength(self):
        return self.ZMax - self.ZMin

    @property
    def Center(self):
        return _Vector(
            (self.XMin + self.XMax) / 2,
            (self.YMin + self.YMax) / 2,
            (self.ZMin + self.ZMax) / 2,
        )

    def isValid(self):
        return True

    def add(self, other):
        self.XMin, self.XMax = min(self.XMin, other.XMin), max(self.XMax, other.XMax)
        self.YMin, self.YMax = min(self.YMin, other.YMin), max(self.YMax, other.YMax)
        self.ZMin, self.ZMax = min(self.ZMin, other.ZMin), max(self.ZMax, other.ZMax)


class _Edge:
    """An edge along +X through the origin, so the feasibility maths is legible."""

    def __init__(self):
        self.CenterOfMass = _Vector(0.0, 0.0, 0.0)
        self.ParameterRange = (0.0, 1.0)

    def tangentAt(self, _parameter):
        return _Vector(1.0, 0.0, 0.0)

    def isSame(self, other):
        return other is self


class _Surface:
    def parameter(self, _point):
        return (0.0, 0.0)


class _Vertex:
    def __init__(self, point):
        self.Point = point


class _Face:
    def __init__(self, normal, corners):
        self.Orientation = "Forward"
        self.Edges = []
        self.Vertexes = [_Vertex(corner) for corner in corners]
        self.Surface = _Surface()
        self._normal = normal
        box = _BoundBox()
        for corner in corners:
            box.add(_BoundBox(corner, corner))
        self.BoundBox = box

    def normalAt(self, _u, _v):
        return self._normal


class _Shape:
    def __init__(self, volume=1000.0, solids=1, edges=12, extent=FACE_EXTENT):
        self.ShapeType = "Solid"
        self.Volume = volume
        self.Area = 6.0
        self.Length = 12.0
        self.Solids = [object()] * solids
        self.Shells = [object()]
        self.Faces = _faces(extent)
        self.Edges = [_Edge() for _ in range(edges)]
        self.Vertexes = [object()] * 8
        self.BoundBox = _BoundBox((0.0, 0.0, 0.0), (extent, extent, extent))
        self._valid = True

        for face in self.Faces:
            face.Edges = list(self.Edges)

    def isNull(self):
        return False

    def isValid(self):
        return self._valid

    def isClosed(self):
        return True

    def transformed(self, matrix):
        copy = _Shape(volume=self.Volume, solids=len(self.Solids))
        copy.BoundBox = _BoundBox(
            (
                self.BoundBox.XMin + matrix.offset.x,
                self.BoundBox.YMin + matrix.offset.y,
                self.BoundBox.ZMin + matrix.offset.z,
            ),
            (
                self.BoundBox.XMax + matrix.offset.x,
                self.BoundBox.YMax + matrix.offset.y,
                self.BoundBox.ZMax + matrix.offset.z,
            ),
        )
        return copy


def _faces(extent):
    """Two faces sharing every edge, spanning ``extent`` from it.

    One lies in the XZ plane (normal +Y), the other in the XY plane
    (normal +Z). With the edge running along +X, each face is ``extent`` wide
    measured perpendicular to the edge, so the feasibility bound is ``extent``.
    """
    span = extent
    return [
        _Face(
            _Vector(0.0, 1.0, 0.0),
            [
                _Vector(0.0, 0.0, 0.0),
                _Vector(span, 0.0, 0.0),
                _Vector(0.0, 0.0, -span),
                _Vector(span, 0.0, -span),
            ],
        ),
        _Face(
            _Vector(0.0, 0.0, 1.0),
            [
                _Vector(0.0, 0.0, 0.0),
                _Vector(span, 0.0, 0.0),
                _Vector(0.0, span, 0.0),
                _Vector(span, span, 0.0),
            ],
        ),
    ]


class _Matrix:
    """Just enough of a placement matrix to offset a bounding box."""

    def __init__(self, offset=None):
        self.offset = offset or _Vector()

    def __mul__(self, other):
        return _Matrix(self.offset + other.offset)


class _Rotation:
    def __init__(self, axis=(0.0, 0.0, 1.0), angle=0.0):
        self.Axis = _Vector(*axis)
        self.Angle = float(angle)


class _Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base if base is not None else _Vector()
        self.Rotation = rotation if rotation is not None else _Rotation()

    def toMatrix(self):
        return _Matrix(self.Base)


class _Object:
    def __init__(self, type_id, name):
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        self.Shape = None
        self.Edges = []
        self.Base = None
        self.Source = None
        self.Normal = None
        self.MirrorPlane = None
        self.Placement = _Placement()
        self.OutList = []
        self.InList = []


class _Document:
    def __init__(self, name):
        self.Name = name
        self.Label = name
        self.FileName = ""
        self.Objects = []
        self.hooks = []

    def addObject(self, type_id, name):
        obj = _Object(type_id, name)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        for obj in self.Objects:
            if obj.Name == name:
                return obj
        return None

    def recompute(self):
        for hook in list(self.hooks):
            hook(self)

    def save(self):
        return True


class _App(types.ModuleType):
    def __init__(self):
        super().__init__("FreeCAD")
        self.Vector = _Vector
        self.Placement = _Placement
        self.Rotation = _Rotation
        self.Matrix = _Matrix
        self._documents = {}

    def Version(self):
        return [HOST_VERSION.split(".")[0], HOST_VERSION.split(".")[1], HOST_VERSION.split(".")[2]]

    def openDocument(self, path):
        doc = self._documents.setdefault(path, _Document(Path(path).stem))
        return doc

    def newDocument(self, name):
        doc = _Document(name)
        self._documents[name] = doc
        return doc

    def closeDocument(self, name):
        self._documents.pop(name, None)


class _Part(types.ModuleType):
    def __init__(self):
        super().__init__("Part")

    def makeCompound(self, shapes):
        compound = _Shape(volume=0.0, solids=0)
        compound.Solids = [solid for shape in shapes for solid in shape.Solids]
        compound.Volume = sum(shape.Volume for shape in shapes)
        box = _BoundBox()
        for shape in shapes:
            box.add(shape.BoundBox)
        compound.BoundBox = box
        return compound


@pytest.fixture
def host(monkeypatch):
    """Install the fake FreeCAD and return a harness over one document.

    ``hooks`` are callbacks the fake document runs on every recompute, which is
    how a test makes the host silently drop a change after accepting it.
    """
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_write_contract"] = write_contract
    app = _App()
    part = _Part()
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    monkeypatch.setitem(sys.modules, "Part", part)
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})

    path = "staged.FCStd"
    doc = app.openDocument(path)
    source = doc.addObject("Part::Box", "Body")
    source.Shape = _Shape(volume=1000.0)

    # Stand in for the kernel: a result that was asked for comes back with a
    # shape. It differs from the source volume because every dress-up is
    # supposed to change the volume, and tests override it when they want a
    # specific check to fire.
    def default_shape(document):
        for obj in document.Objects:
            if obj.Shape is None:
                obj.Shape = _Shape(volume=900.0)

    doc.hooks.append(default_shape)
    return types.SimpleNamespace(
        app=app, part=part, doc=doc, path=path, source=source, hooks=doc.hooks
    )


def _params(host, **extra):
    params = {"document_path": host.path, "object_name": "Body", "result_name": "Result"}
    params.update(extra)
    return params


# ---------------------------------------------------------------------------
# The checks fire
# ---------------------------------------------------------------------------


def test_fillet_refuses_when_the_host_drops_the_edge_write(host):
    """An accepted call whose edge list never landed is not a success."""

    def drop(doc):
        doc.getObject("Result").Edges = []

    host.hooks.append(drop)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_fillet_edges(_params(host, edge_refs=[1, 2], radius=2.0))

    assert excinfo.value.check == "result.edges[1]"
    assert excinfo.value.expected == [2.0, 2.0]


def test_fillet_refuses_when_the_result_is_not_wired_to_the_source(host):
    def unwire(doc):
        doc.getObject("Result").Base = None

    host.hooks.append(unwire)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_fillet_edges(_params(host, edge_refs=[1], radius=2.0))

    assert excinfo.value.check == "result.base"


def test_fillet_refuses_when_the_volume_did_not_move(host):
    def unshaped(doc):
        result = doc.getObject("Result")
        result.Shape = _Shape(volume=host.source.Shape.Volume)

    host.hooks.append(unshaped)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_fillet_edges(_params(host, edge_refs=[1, 2], radius=2.0))

    assert excinfo.value.check == "result.volume_changed"


def test_linear_pattern_refuses_when_an_instance_is_dropped(host):
    """Three instances were asked for; two arriving is not a pattern."""

    def drop_one(doc):
        # Absolute rather than a slice: the driver recomputes once to build and
        # again to save, so a relative drop would land twice.
        result = doc.getObject("Result")
        result.Shape.Solids = result.Shape.Solids[:2]

    host.hooks.append(drop_one)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_linear_pattern(
            _params(host, direction=[1, 0, 0], spacing=60.0, count=3)
        )

    assert excinfo.value.check == "result.instance_count"
    assert excinfo.value.expected == 3
    assert excinfo.value.actual == 2


def test_linear_pattern_refuses_when_the_volume_does_not_sum(host):
    def short_change(doc):
        doc.getObject("Result").Shape.Volume = 1000.0

    host.hooks.append(short_change)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_linear_pattern(
            _params(host, direction=[1, 0, 0], spacing=60.0, count=3)
        )

    assert excinfo.value.check == "result.volume"
    assert excinfo.value.expected == 3000.0


def test_mirror_refuses_when_the_volume_moves(host):
    """A mirror that changes the volume did not mirror."""

    def shrink(doc):
        doc.getObject("Result").Shape = _Shape(volume=999.0)

    host.hooks.append(shrink)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_mirror_feature(_params(host, plane="yz"))

    assert excinfo.value.check == "result.volume"
    assert excinfo.value.expected == 1000.0


# ---------------------------------------------------------------------------
# The feasibility bound runs before the kernel is asked
# ---------------------------------------------------------------------------


def test_infeasible_radius_is_refused_without_creating_an_object(host):
    """The pre-check is a pre-check: the kernel is never asked.

    A bound that runs after the host has already built a self-intersecting solid
    is not a pre-check, and the object count is what proves the difference. The
    message also has to name a limit, or the caller has nothing to retry with.
    """
    before = len(host.doc.Objects)

    with pytest.raises(ValueError) as excinfo:
        freecad_driver.model_fillet_edges(_params(host, edge_refs=[1], radius=FACE_EXTENT * 2))

    assert getattr(excinfo.value, "code", None) == "E_RADIUS_NOT_FEASIBLE"
    assert "absorbs below %g" % FACE_EXTENT in str(excinfo.value)
    assert len(host.doc.Objects) == before, "the kernel must not have been asked"


def test_a_feasible_radius_still_reaches_the_kernel(host):
    """The other side of the bound: a radius that fits must not be refused."""
    result = freecad_driver.model_fillet_edges(_params(host, edge_refs=[1], radius=2.0))

    assert result["affected_edges"] == 1
    assert "result.edges[1]" in result["verified"]


def test_chamfer_reports_both_distances_in_its_refusal(host):
    with pytest.raises(ValueError) as excinfo:
        freecad_driver.model_chamfer_edges(
            _params(host, edge_refs=[1], distance1=FACE_EXTENT * 2, distance2=1.0)
        )

    assert getattr(excinfo.value, "code", None) == "E_DISTANCE_NOT_FEASIBLE"


def test_edge_ref_refusals_are_coded(host):
    """Every malformed edge reference has its own code, not one generic one."""
    for refs, code in (
        ([0], "E_EDGE_REF_OUT_OF_RANGE"),
        ([13], "E_EDGE_REF_OUT_OF_RANGE"),
        ([1, 1], "E_EDGE_REF_DUPLICATE"),
        (list(range(1, 202)), "E_EDGE_REF_LIMIT"),
        ([True], "E_EDGE_REF_INVALID"),
        (["1"], "E_EDGE_REF_INVALID"),
    ):
        with pytest.raises(ValueError) as excinfo:
            freecad_driver.model_fillet_edges(_params(host, edge_refs=refs, radius=1.0))
        assert getattr(excinfo.value, "code", None) == code, refs


def test_pattern_limits_are_coded(host):
    with pytest.raises(ValueError) as excinfo:
        freecad_driver.model_linear_pattern(
            _params(host, direction=[1, 0, 0], spacing=10.0, count=1001)
        )
    assert getattr(excinfo.value, "code", None) == "E_INSTANCE_LIMIT"

    with pytest.raises(ValueError) as excinfo:
        freecad_driver.model_linear_pattern(
            _params(host, direction=[0, 0, 0], spacing=10.0, count=3)
        )
    assert getattr(excinfo.value, "code", None) == "E_ZERO_VECTOR"

    with pytest.raises(ValueError) as excinfo:
        freecad_driver.model_linear_pattern(
            _params(host, direction=[1, 0, 0], spacing=0.0, count=3)
        )
    assert getattr(excinfo.value, "code", None) == "E_PATTERN_SPACING"
