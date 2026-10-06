"""Recipe/schema checks only; not native model or rendering qualification."""

import importlib.util
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[1]


def test_original_actuator_uses_only_supported_typed_operations_and_real_checkpoints():
    spec = importlib.util.spec_from_file_location(
        "actuator_scene", str(ROOT / "docs/demos/actuator_scene.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scene = module.recipe()
    declarations = {}
    for skill in ("freecad-session", "freecad-modeling"):
        rows = yaml.safe_load(
            (ROOT / "src/dcc_mcp_freecad/skills" / skill / "tools.yaml").read_text()
        )["tools"]
        declarations.update({row["name"]: row for row in rows})
    names = set()
    counts = []
    for phase, calls in scene["phases"].items():
        children = 0
        for call in calls:
            tool, args = call["tool"], call["arguments"]
            Draft7Validator(declarations[tool]["input_schema"]).validate(args)
            children += (
                2 if tool in ("add_primitive", "boolean_operation", "transform_object") else 1
            )
            if tool == "add_primitive":
                assert args["name"] not in names
                names.add(args["name"])
            elif tool == "boolean_operation":
                assert {args["base_object"], args["tool_object"]} <= names
                assert args["result_name"] not in names
                names.add(args["result_name"])
            elif tool == "transform_object":
                assert args["object_name"] in names
            elif tool == "save_copy":
                assert set(args["visible_objects"]) <= names
                assert args["overwrite"] is False
        assert [call["tool"] for call in calls[-4:]] == [
            "validate_document",
            "inspect_document",
            "save_copy",
            "inspect_document",
        ]
        counts.append((phase, len(calls), children))
    assert len(names) == 27 and len(scene["final_visible_objects"]) == 17
    assert counts == [
        ("body", 12, 19),
        ("mechanism", 8, 12),
        ("assembly", 20, 36),
        ("stroke", 8, 12),
    ]
    assert scene["kinematic_joint_or_manufacturing_claim"] is False
