"""Post-write read-back contract: every mutating tool proves its own effect.

The failure mode under test is "reported success, model unchanged". Each
mutating method therefore has a case where the host silently drops the write --
the exact shape of that bug -- and the test asserts the tool refuses to return
instead of handing the caller a plausible-looking payload.

The fake FreeCAD below is deliberately small: it stores what it is told, so a
write the host drops is observably missing. The real-host lane in
``tests/test_bridge.py`` is what proves these checks do not fire on real
geometry; this file proves they do fire when a write does not stick.
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
    MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    freecad_driver,
    write_contract,
)

HOST_VERSION = "1.1.4"


# ---------------------------------------------------------------------------
# Contract primitives (stdlib only: the reusable half of the contract)
# ---------------------------------------------------------------------------


def test_message_states_tool_check_and_both_sides():
    error = write_contract.WriteVerificationError(
        tool="model.update_primitive",
        check="dimension.Length",
        expected=84.0,
        actual=80.0,
        host_version="1.1.4",
    )

    message = str(error)

    assert "model.update_primitive" in message
    assert "dimension.Length" in message
    assert "84.0" in message and "80.0" in message
    assert "1.1.4" in message


def test_error_payload_round_trips_across_a_process_boundary():
    error = write_contract.WriteVerificationError(
        tool="model.transform_object",
        check="object.placement",
        expected={"translation": [1.0, 2.0, 3.0]},
        actual={"translation": [0.0, 0.0, 0.0]},
        host_version="1.0.2",
        host_matrix={"status": "supported", "range_id": "1.0.x"},
        params={"object_name": "Body"},
    )

    revived = write_contract.WriteVerificationError.from_payload(
        json.loads(json.dumps(error.payload))
    )

    assert revived.payload == error.payload
    assert str(revived) == str(error)


def test_numbers_and_sequences_discriminate_real_differences():
    assert write_contract.numbers_match(84.0, 84.0 + 1e-12)
    assert not write_contract.numbers_match(84.0, 80.0)
    assert not write_contract.numbers_match(84.0, None)
    assert write_contract.sequences_match([1, 2, 3], (1.0, 2.0, 3.0))
    assert not write_contract.sequences_match([1, 2, 3], [1, 2])
    assert not write_contract.sequences_match([1, 2, 3], [1, 2, 4])


def test_non_finite_values_survive_serialisation():
    payload = write_contract.WriteVerificationError(
        tool="model.add_primitive", check="dimension.Radius", expected=1.0, actual=float("nan")
    ).payload

    assert payload["actual"] == "nan"
    assert json.loads(json.dumps(payload))["actual"] == "nan"


def test_every_driver_method_is_classified_as_mutating_or_read_only():
    methods = set(freecad_driver._METHODS)

    assert methods == set(MUTATING_TOOLS) | set(READ_ONLY_TOOLS), (
        write_contract.TOOL_CLASSIFICATION_ERROR
    )
    assert not (set(MUTATING_TOOLS) & set(READ_ONLY_TOOLS))


def test_mutating_tools_are_the_ones_that_change_state():
    # system.status is the instrument that measures the host, so it must never
    # be the thing that cannot report. Everything else that can move geometry or
    # write a file owes a read-back.
    assert "system.status" not in MUTATING_TOOLS
    for method in (
        "model.add_primitive",
        "model.update_primitive",
        "model.transform_object",
        "model.boolean_operation",
        "model.import_geometry",
        "model.export_geometry",
        "document.remove_object",
    ):
        assert method in MUTATING_TOOLS, method


# ---------------------------------------------------------------------------
# A FreeCAD that stores what it is told, so a dropped write is observable
# ---------------------------------------------------------------------------


class _Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __repr__(self):
        return "Vector(%r, %r, %r)" % (self.x, self.y, self.z)


class _Rotation:
    def __init__(self, axis=(0.0, 0.0, 1.0), angle=0.0):
        self.Axis = _Vec(*axis)
        self.Angle = float(angle)

    def isSame(self, other, tolerance=0.0):
        return list(self.Axis) == list(other.Axis) and abs(self.Angle - other.Angle) <= (
            tolerance or 1e-9
        )

    def __repr__(self):
        return "Rotation(%r, %r)" % (list(self.Axis), self.Angle)


class _Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base if base is not None else _Vec()
        self.Rotation = rotation if rotation is not None else _Rotation()

    def multVec(self, vector):
        return _Vec(vector.x + self.Base.x, vector.y + self.Base.y, vector.z + self.Base.z)

    def __repr__(self):
        return "Placement(%r, %r)" % (self.Base, self.Rotation)


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
        return _Vec(
            (self.XMin + self.XMax) / 2,
            (self.YMin + self.YMax) / 2,
            (self.ZMin + self.ZMax) / 2,
        )

    def isValid(self):
        return True

    def add(self, other):
        self.XMin = min(self.XMin, other.XMin)
        self.YMin = min(self.YMin, other.YMin)
        self.ZMin = min(self.ZMin, other.ZMin)
        self.XMax = max(self.XMax, other.XMax)
        self.YMax = max(self.YMax, other.YMax)
        self.ZMax = max(self.ZMax, other.ZMax)


class _Shape:
    def __init__(self, volume=1.0, solids=1, valid=True, null=False, box=None):
        self.ShapeType = "Solid"
        self.Volume = volume
        self.Area = 6.0
        self.Length = 12.0
        self.Solids = [object()] * solids
        self.Shells = [object()]
        self.Faces = [object()] * 6
        self.Edges = [object()] * 12
        self.Vertexes = [object()] * 8
        self.BoundBox = box if box is not None else _BoundBox()
        self._valid = valid
        self._null = null

    def isNull(self):
        return self._null

    def isValid(self):
        return self._valid

    def isClosed(self):
        return True


class _Mesh:
    def __init__(self, points=8, facets=12):
        self.CountPoints = points
        self.CountFacets = facets
        self.Volume = 1.0
        self.Area = 6.0
        self.BoundBox = _BoundBox()

    def copy(self):
        return _Mesh(self.CountPoints, self.CountFacets)


class _Object:
    """A document object that stores writes, unless ``silenced``.

    ``silenced`` models the bug class this contract exists for: the host accepts
    the write and returns, but nothing changes. Identity attributes stay
    writable because an object that loses its type or shape is a different
    failure, not a dropped write.
    """

    _IDENTITY = frozenset({"TypeId", "Name", "Shape", "OutList", "InList", "_silenced", "_doc"})

    def __init__(self, type_id, name, doc):
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        self.Placement = _Placement()
        self.OutList = ()
        self.InList = ()
        self.Shape = None
        self._silenced = False
        self._doc = doc

    def __setattr__(self, key, value):
        if key in _Object._IDENTITY or key.startswith("__"):
            object.__setattr__(self, key, value)
            return
        if getattr(self, "_silenced", False):
            return
        object.__setattr__(self, key, value)


class _Document:
    def __init__(self, name, path="", app=None):
        self.Name = name
        self.Label = name
        self.FileName = path
        self.Objects = []
        self._app = app

    def addObject(self, type_id, name):
        obj = _Object(type_id, name, self)
        if type_id == "Mesh::Feature":
            obj.Mesh = _Mesh()
        elif type_id.startswith("Part::"):
            obj.Shape = _Shape()
            # A real primitive exposes its dimension properties from creation.
            for prop in freecad_driver._DIMENSION_PROPERTIES.get(type_id, {}).values():
                setattr(obj, prop, 1.0)
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
        for obj in self.Objects:
            if obj.Shape is None and obj.TypeId.startswith("Part::"):
                obj.Shape = _Shape()

    def save(self):
        pass

    def saveAs(self, path):
        self.FileName = path
        Path(path).write_bytes(b"fake-fcstd")
        if self._app is not None:
            # Remember what was written, so reopening the file gives it back.
            self._app.saved[str(path)] = [obj.Name for obj in self.Objects]


class _App:
    """The subset of the FreeCAD module the driver touches."""

    def __init__(self, version=HOST_VERSION):
        # App.Version() carries more fields; the driver reports the first three.
        major, minor, patch = (version.split(".") + ["0", "0"])[:3]
        self.Version = lambda: [major, minor, patch, "extra"]
        self.documents = {}
        self.saved = {}
        self.clone_on_open = True
        self.Vector = _Vec
        self.Rotation = _Rotation
        self.Placement = _Placement

    def newDocument(self, name):
        doc = _Document(name, "", self)
        self.documents[name] = doc
        return doc

    def openDocument(self, path):
        key = str(path)
        if key in self.documents:
            return self.documents[key]
        doc = _Document(Path(key).stem, key, self)
        if self.clone_on_open:
            for name in self.saved.get(key, ()):
                doc.addObject("Part::Box", name)
        self.documents[key] = doc
        return doc

    def closeDocument(self, name):
        self.documents.pop(name, None)


class FakePart(types.ModuleType):
    """Stands in for FreeCAD's ``Part`` module."""

    def __init__(self):
        super().__init__("Part")
        self.reads = []
        self.exports = []
        self.shapes = {}

    def read(self, path):
        self.reads.append(path)
        shape = self.shapes.get(str(path))
        if shape is None:
            shape = _Shape()
            self.shapes[str(path)] = shape
        return shape

    def export(self, objects, path):
        self.exports.append(([obj.Name for obj in objects], path))
        Path(path).write_bytes(b"fake-step")


class FakeMesh(types.ModuleType):
    """Stands in for FreeCAD's ``Mesh`` module."""

    def __init__(self, points=8, facets=12):
        super().__init__("Mesh")
        self.points = points
        self.facets = facets
        self.exports = []
        self.Mesh = self._mesh_factory

    def _mesh_factory(self, path=None):
        if path is not None and not Path(path).is_file():
            raise IOError("cannot open %s" % path)
        return _Mesh(self.points, self.facets)

    def export(self, objects, path):
        self.exports.append(([obj.Name for obj in objects], path))
        Path(path).write_bytes(b"fake-stl")


class FakeMeshPart(types.ModuleType):
    def __init__(self):
        super().__init__("MeshPart")
        # Recorded so a test can prove which deflection actually reached the
        # host, instead of inferring it from the exported bytes.
        self.calls = []

    def meshFromShape(self, **kwargs):
        self.calls.append(kwargs)
        return _Mesh()


@pytest.fixture()
def host(monkeypatch):
    """Install a fake FreeCAD and return the harness.

    The driver loads ``write_contract.py`` by path, so that module object is not
    the one imported here. Seeding the driver's cache with the imported module
    makes them the same object, which is what lets a test catch the exact error
    type the driver raises.
    """
    freecad_driver._SIBLING_MODULES["dcc_mcp_freecad_write_contract"] = write_contract
    app = _App()
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    part = FakePart()
    mesh = FakeMesh()
    mesh_part = FakeMeshPart()
    monkeypatch.setitem(sys.modules, "Part", part)
    monkeypatch.setitem(sys.modules, "Mesh", mesh)
    monkeypatch.setitem(sys.modules, "MeshPart", mesh_part)
    monkeypatch.setattr(freecad_driver, "host_matrix", lambda version: {"status": "supported"})
    return types.SimpleNamespace(app=app, part=part, mesh=mesh, mesh_part=mesh_part)


def _document(host, tmp_path, name="model"):
    path = tmp_path / ("%s.FCStd" % name)
    path.write_bytes(b"document")
    return host.app.openDocument(str(path)), str(path)


def _new_objects_with_shape(doc, shape):
    """Make every object created from here on carry ``shape``.

    Models the host behaviour the shape read-back is the only guard against:
    the property writes land, the object exists with the requested dimensions,
    and the geometry behind it is unusable. Nothing in the dimension or
    placement checks can see that, so without this fixture the shape check is
    the one assertion in the primitive read-back no fast-lane test exercises.
    """
    original = doc.addObject

    def addObject(type_id, name):
        obj = original(type_id, name)
        obj.Shape = shape
        return obj

    doc.addObject = addObject
    return original


def _silence_new_objects(doc):
    """Make every object created from here on silently drop its writes."""
    original = doc.addObject

    def addObject(type_id, name):
        obj = original(type_id, name)
        if type_id == "Mesh::Feature":
            # An object that exists but carries no geometry: the write did not
            # stick, which is exactly what the read-back has to notice.
            obj.Mesh = None
        obj._silenced = True
        return obj

    doc.addObject = addObject
    return original


def _mismatch(excinfo):
    """Assert the failure is a structured read-back mismatch and return it.

    Both sides of the comparison are required to be present, not merely absent:
    a mismatch that only names the tool is the "failed, go guess" report this
    contract exists to eliminate.
    """
    error = excinfo.value
    assert isinstance(error, write_contract.WriteVerificationError), type(error)
    assert error.tool, "the error must name the tool"
    assert error.check, "the error must name the check that disagreed"
    assert "expected" in error.payload and "actual" in error.payload
    assert error.host_version == HOST_VERSION, "the host version travels with the mismatch"
    return error


# ---------------------------------------------------------------------------
# document.create
# ---------------------------------------------------------------------------


def test_create_document_reads_back_a_durable_artifact(host, tmp_path):
    output = tmp_path / "created.FCStd"

    result = freecad_driver.document_create({"name": "Created", "document_path": str(output)})

    assert "artifact.non_empty" in result["verified"]
    assert output.is_file()


def test_create_document_fails_when_nothing_was_written(host, tmp_path):
    output = tmp_path / "created.FCStd"
    doc = _Document("Created", str(output), host.app)
    doc.saveAs = lambda path: None  # silently writes nothing
    host.app.newDocument = lambda name: doc

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.document_create({"name": "Created", "document_path": str(output)})

    assert _mismatch(excinfo).check == "artifact.non_empty"


def test_create_document_rejects_a_missing_parameter(host, tmp_path):
    with pytest.raises(ValueError, match="requires parameter 'name'"):
        freecad_driver.document_create({"document_path": str(tmp_path / "a.FCStd")})


# ---------------------------------------------------------------------------
# model.add_primitive
# ---------------------------------------------------------------------------


def test_add_primitive_reports_the_checks_it_ran(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    result = freecad_driver.model_add_primitive(
        {
            "document_path": path,
            "primitive": "box",
            "name": "Body",
            "dimensions": {"length": 80, "width": 50, "height": 24},
            "translation": [1, 2, 3],
        }
    )

    assert result["object"]["name"] == "Body"
    for check in ("object.exists", "dimension.Length", "object.type_id", "object.placement"):
        assert check in result["verified"], check


def test_add_primitive_refuses_when_a_dimension_did_not_land(host, tmp_path):
    doc, path = _document(host, tmp_path)
    _silence_new_objects(doc)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_add_primitive(
            {
                "document_path": path,
                "primitive": "box",
                "name": "Body",
                "dimensions": {"length": 84},
            }
        )

    error = _mismatch(excinfo)
    assert error.check == "dimension.Length"
    assert error.expected == 84.0


def test_add_primitive_refuses_when_the_placement_did_not_land(host, tmp_path):
    doc, path = _document(host, tmp_path)
    _silence_new_objects(doc)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_add_primitive(
            {
                "document_path": path,
                "primitive": "box",
                "name": "Body",
                "translation": [10, 20, 30],
            }
        )

    assert _mismatch(excinfo).check == "object.placement"


def test_add_primitive_refuses_an_object_with_no_geometry(host, tmp_path):
    doc, path = _document(host, tmp_path)
    _new_objects_with_shape(doc, _Shape(null=True))

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_add_primitive(
            {
                "document_path": path,
                "primitive": "box",
                "name": "Body",
                "dimensions": {"length": 80, "width": 50, "height": 24},
            }
        )

    error = _mismatch(excinfo)
    assert error.check == "object.shape.not_null"
    assert error.actual == "null"


def test_add_primitive_rejects_a_dimension_the_primitive_does_not_have(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="Unsupported dimensions for Part::Box: radius"):
        freecad_driver.model_add_primitive(
            {"document_path": path, "primitive": "box", "name": "Body", "dimensions": {"radius": 5}}
        )


def test_add_primitive_rejects_dimensions_that_are_not_a_mapping(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="dimensions must be an object"):
        freecad_driver.model_add_primitive(
            {"document_path": path, "primitive": "box", "name": "Body", "dimensions": [80, 50]}
        )


def test_add_primitive_rejects_an_unknown_primitive(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="Unsupported primitive: blob"):
        freecad_driver.model_add_primitive(
            {"document_path": path, "primitive": "blob", "name": "Body"}
        )


def test_add_primitive_rejects_a_degenerate_rotation_axis(host, tmp_path):
    _doc, path = _document(host, tmp_path)

    with pytest.raises(ValueError, match="rotation_axis may not be the zero vector"):
        freecad_driver.model_add_primitive(
            {
                "document_path": path,
                "primitive": "box",
                "name": "Body",
                "rotation_axis": [0, 0, 0],
            }
        )


# ---------------------------------------------------------------------------
# model.update_primitive
# ---------------------------------------------------------------------------


def test_update_primitive_reads_the_new_dimension_back(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.addObject("Part::Box", "Body")
    body.Length = 80.0

    result = freecad_driver.model_update_primitive(
        {"document_path": path, "object_name": "Body", "dimensions": {"length": 84}}
    )

    assert result["object"]["name"] == "Body"
    assert "dimension.Length" in result["verified"]
    assert body.Length == 84.0


def test_update_primitive_refuses_a_silently_dropped_write(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.addObject("Part::Box", "Body")
    body.Length = 80.0
    body._silenced = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_update_primitive(
            {"document_path": path, "object_name": "Body", "dimensions": {"length": 84}}
        )

    error = _mismatch(excinfo)
    assert error.check == "dimension.Length"
    assert error.expected == 84.0
    assert error.actual == 80.0


def test_update_primitive_refuses_an_object_whose_shape_is_invalid(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.addObject("Part::Box", "Body")
    body.Shape = _Shape(valid=False)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_update_primitive(
            {"document_path": path, "object_name": "Body", "dimensions": {"length": 84}}
        )

    error = _mismatch(excinfo)
    assert error.check == "object.shape.valid"
    assert error.expected is True
    assert error.actual is False


def test_update_primitive_reports_the_label_it_failed_to_persist(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")._silenced = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_update_primitive(
            {"document_path": path, "object_name": "Body", "label": "Housing"}
        )

    error = _mismatch(excinfo)
    assert error.check == "object.label"
    assert error.expected == "Housing"


# ---------------------------------------------------------------------------
# model.transform_object
# ---------------------------------------------------------------------------


def test_transform_object_proves_the_new_placement(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")

    result = freecad_driver.model_transform_object(
        {
            "document_path": path,
            "object_name": "Body",
            "translation": [42, 25, 12],
            "rotation_axis": [0, 1, 0],
            "rotation_degrees": 90,
        }
    )

    assert "object.placement" in result["verified"]
    assert list(result["object"]["placement"]["translation"]) == [42, 25, 12]


def test_transform_object_refuses_when_the_object_did_not_move(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")._silenced = True

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_transform_object(
            {"document_path": path, "object_name": "Body", "translation": [42, 25, 12]}
        )

    error = _mismatch(excinfo)
    assert error.check == "object.placement"
    assert error.expected["translation"] == [42, 25, 12]
    assert error.actual["translation"] == [0, 0, 0]


def test_transform_object_rejects_a_short_translation(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")

    with pytest.raises(ValueError, match="translation must contain exactly three numbers"):
        freecad_driver.model_transform_object(
            {"document_path": path, "object_name": "Body", "translation": [1, 2]}
        )


# ---------------------------------------------------------------------------
# model.boolean_operation
# ---------------------------------------------------------------------------


def _boolean_document(host, tmp_path):
    doc, path = _document(host, tmp_path)
    base = doc.addObject("Part::Box", "Body")
    base.Shape = _Shape(volume=100.0)
    operand = doc.addObject("Part::Cylinder", "PortCut")
    operand.Shape = _Shape(volume=10.0)
    return doc, path, base, operand


def test_boolean_operation_proves_the_operands_are_wired(host, tmp_path):
    _doc, path, _base, _operand = _boolean_document(host, tmp_path)

    result = freecad_driver.model_boolean_operation(
        {
            "document_path": path,
            "operation": "cut",
            "base_object": "Body",
            "tool_object": "PortCut",
            "result_name": "BodyWithPort",
        }
    )

    for check in ("result.exists", "result.base", "result.tool", "result.volume_relation"):
        assert check in result["verified"], check


def test_boolean_operation_refuses_when_the_operand_links_did_not_land(host, tmp_path):
    doc, path, _base, _operand = _boolean_document(host, tmp_path)
    original = doc.addObject

    def addObject(type_id, name):
        obj = original(type_id, name)
        if name == "BodyWithPort":
            # The feature exists but its operand wiring was dropped: the classic
            # "reported success, geometry unchanged" boolean.
            obj._silenced = True
        return obj

    doc.addObject = addObject

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_boolean_operation(
            {
                "document_path": path,
                "operation": "cut",
                "base_object": "Body",
                "tool_object": "PortCut",
                "result_name": "BodyWithPort",
            }
        )

    assert _mismatch(excinfo).check == "result.base"


def test_boolean_operation_refuses_a_volume_that_contradicts_the_operation(host, tmp_path):
    doc, path, _base, _operand = _boolean_document(host, tmp_path)
    original = doc.addObject

    def addObject(type_id, name):
        obj = original(type_id, name)
        if name == "BodyWithPort":
            # A cut that came back larger than its base cannot be that cut.
            obj.Shape = _Shape(volume=500.0)
        return obj

    doc.addObject = addObject

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_boolean_operation(
            {
                "document_path": path,
                "operation": "cut",
                "base_object": "Body",
                "tool_object": "PortCut",
                "result_name": "BodyWithPort",
            }
        )

    error = _mismatch(excinfo)
    assert error.check == "result.volume_relation"
    assert error.actual["result"] == 500.0


def test_boolean_operation_rejects_an_unknown_operation(host, tmp_path):
    _doc, path, _base, _operand = _boolean_document(host, tmp_path)

    with pytest.raises(ValueError, match="Unsupported boolean operation: merge"):
        freecad_driver.model_boolean_operation(
            {
                "document_path": path,
                "operation": "merge",
                "base_object": "Body",
                "tool_object": "PortCut",
                "result_name": "BodyWithPort",
            }
        )


# ---------------------------------------------------------------------------
# document.remove_object
# ---------------------------------------------------------------------------


def test_remove_object_proves_every_name_is_gone(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")

    result = freecad_driver.document_remove_object({"document_path": path, "object_name": "Body"})

    assert result["removed_objects"] == ["Body"]
    assert "object.removed" in result["verified"]


def test_remove_object_refuses_when_the_object_survived(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    doc.removeObject = lambda name: None  # silently keeps the object

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.document_remove_object({"document_path": path, "object_name": "Body"})

    error = _mismatch(excinfo)
    assert error.check == "object.removed"
    assert "Body" in error.actual


# ---------------------------------------------------------------------------
# model.import_geometry
# ---------------------------------------------------------------------------


def test_import_geometry_proves_the_geometry_landed(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    source = tmp_path / "model.step"
    source.write_bytes(b"solid")

    result = freecad_driver.model_import_geometry(
        {"document_path": path, "input_path": str(source), "object_name": "ImportedBody"}
    )

    assert result["object"]["name"] == "ImportedBody"
    for check in ("input.non_empty", "object.exists", "object.shape.not_null"):
        assert check in result["verified"], check


def test_import_geometry_refuses_an_empty_source(host, tmp_path):
    _doc, path = _document(host, tmp_path)
    source = tmp_path / "model.step"
    source.write_bytes(b"")

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_import_geometry(
            {"document_path": path, "input_path": str(source), "object_name": "ImportedBody"}
        )

    assert _mismatch(excinfo).check == "input.non_empty"


def test_import_geometry_refuses_when_the_mesh_came_back_empty(host, tmp_path):
    doc, path = _document(host, tmp_path)
    source = tmp_path / "model.stl"
    source.write_bytes(b"solid")
    _silence_new_objects(doc)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_import_geometry(
            {"document_path": path, "input_path": str(source), "object_name": "ImportedMesh"}
        )

    error = _mismatch(excinfo)
    assert error.check == "mesh.non_empty"
    assert error.actual == {"points": 0, "facets": 0}


# ---------------------------------------------------------------------------
# model.export_geometry
# ---------------------------------------------------------------------------


def test_export_geometry_proves_the_artifact_is_readable(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "model.step"

    result = freecad_driver.model_export_geometry(
        {"document_path": path, "object_names": ["Body"], "output_path": str(output)}
    )

    for check in ("artifact.non_empty", "artifact.shape_not_null", "artifact.solids"):
        assert check in result["verified"], check


def test_export_geometry_refuses_an_unreadable_artifact(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "model.step"

    def explode(read_path):
        raise IOError("unreadable")

    host.part.read = explode

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_export_geometry(
            {"document_path": path, "object_names": ["Body"], "output_path": str(output)}
        )

    error = _mismatch(excinfo)
    assert error.check == "artifact.readable"
    assert "unreadable" in error.actual


def test_export_geometry_refuses_when_the_round_trip_changed_the_volume(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.addObject("Part::Box", "Body")
    body.Shape = _Shape(volume=100.0)
    output = tmp_path / "model.step"
    host.part.shapes[str(output)] = _Shape(volume=42.0)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_export_geometry(
            {"document_path": path, "object_names": ["Body"], "output_path": str(output)}
        )

    error = _mismatch(excinfo)
    assert error.check == "artifact.volume"
    assert error.expected == 100.0
    assert error.actual == 42.0


def test_export_geometry_refuses_a_mesh_outside_the_source_envelope(host, tmp_path):
    doc, path = _document(host, tmp_path)
    body = doc.addObject("Part::Box", "Body")
    body.Shape = _Shape(volume=1.0, box=_BoundBox((0, 0, 0), (10, 10, 10)))
    output = tmp_path / "model.stl"
    escaped = _Mesh()
    escaped.BoundBox = _BoundBox((0, 0, 0), (500, 500, 500))
    host.mesh.Mesh = lambda path=None: escaped

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_export_geometry(
            {"document_path": path, "object_names": ["Body"], "output_path": str(output)}
        )

    assert _mismatch(excinfo).check == "artifact.bounding_box"


def test_export_geometry_keeps_the_documented_default_deflections(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "model.stl"

    result = freecad_driver.model_export_geometry(
        {"document_path": path, "object_names": ["Body"], "output_path": str(output)}
    )

    assert "artifact.mesh_non_empty" in result["verified"]
    assert host.mesh_part.calls[0]["LinearDeflection"] == 0.1
    assert host.mesh_part.calls[0]["AngularDeflection"] == math.radians(15)


def test_export_geometry_refuses_an_explicit_zero_linear_deflection(host, tmp_path):
    """A deflection of 0 is refused, not silently replaced by the default.

    ``params.get("linear_deflection") or 0.1`` accepted the parameter and then
    ignored it, which is the swallowed-parameter behaviour the contract forbids:
    the caller asked for one tessellation and was reported a different one.
    """
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "model.stl"

    with pytest.raises(ValueError, match="linear_deflection must be a finite positive number"):
        freecad_driver.model_export_geometry(
            {
                "document_path": path,
                "object_names": ["Body"],
                "output_path": str(output),
                "linear_deflection": 0,
            }
        )


def test_export_geometry_refuses_an_explicit_zero_angular_deflection(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "model.stl"

    with pytest.raises(
        ValueError, match="angular_deflection_degrees must be a finite positive number"
    ):
        freecad_driver.model_export_geometry(
            {
                "document_path": path,
                "object_names": ["Body"],
                "output_path": str(output),
                "angular_deflection_degrees": 0,
            }
        )


def test_export_geometry_refuses_an_empty_mesh_artifact(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "model.stl"
    host.mesh.points = 0
    host.mesh.facets = 0

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.model_export_geometry(
            {"document_path": path, "object_names": ["Body"], "output_path": str(output)}
        )

    assert _mismatch(excinfo).check == "artifact.mesh_non_empty"


# ---------------------------------------------------------------------------
# document.save_copy
# ---------------------------------------------------------------------------


def test_save_copy_proves_the_copy_reopens(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "copy.FCStd"

    result = freecad_driver.document_save_copy({"document_path": path, "output_path": str(output)})

    assert result["object_names"] == ["Body"]
    assert "copy.objects" in result["verified"]


def test_save_copy_refuses_a_copy_that_lost_objects(host, tmp_path):
    doc, path = _document(host, tmp_path)
    doc.addObject("Part::Box", "Body")
    output = tmp_path / "copy.FCStd"
    host.app.clone_on_open = False  # the copy reopens as an empty document

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        freecad_driver.document_save_copy({"document_path": path, "output_path": str(output)})

    error = _mismatch(excinfo)
    assert error.check == "copy.objects"
    assert error.expected == ["Body"]
    assert error.actual == []


# ---------------------------------------------------------------------------
# The mismatch must reach the caller with its structure intact
# ---------------------------------------------------------------------------


def test_the_driver_forwards_the_structured_payload(host, tmp_path):
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    path = tmp_path / "d.FCStd"
    path.write_bytes(b"document")
    request.write_text(
        json.dumps(
            {
                "method": "model.update_primitive",
                "params": {
                    "document_path": str(path),
                    "object_name": "Body",
                    "dimensions": {"length": 84},
                },
            }
        ),
        encoding="utf-8",
    )
    body = host.app.openDocument(str(path)).addObject("Part::Box", "Body")
    body.Length = 80.0
    body._silenced = True
    sys.argv = ["driver", "--pass", str(request), str(result)]

    freecad_driver.main()

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    verification = payload["error"]["write_verification"]
    assert verification["tool"] == "model.update_primitive"
    assert verification["check"] == "dimension.Length"
    assert verification["expected"] == 84.0
    assert verification["actual"] == 80.0
    assert verification["host_version"] == HOST_VERSION
