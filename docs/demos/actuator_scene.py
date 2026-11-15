"""Original pneumatic-cylinder demo recipe. Produces JSON only; never calls MCP.

The recording coordinator binds DOCUMENT and stage output paths after review.
All geometry is original typed primitives/booleans; no imported third-party CAD.
"""

from __future__ import annotations

import json


def recipe():
    phases = {name: [] for name in ("body", "mechanism", "assembly", "stroke")}

    def add(phase, name, radius, height, position, axis=(0, 1, 0), degrees=90):
        phases[phase].append(
            {
                "tool": "add_primitive",
                "arguments": {
                    "document_path": "{DOCUMENT}",
                    "primitive": "cylinder",
                    "name": name,
                    "dimensions": {"radius": radius, "height": height},
                    "translation": position,
                    "rotation_axis": list(axis),
                    "rotation_degrees": degrees,
                    "timeout_secs": 30,
                },
            }
        )

    def cut(phase, base, tool, name):
        phases[phase].append(
            {
                "tool": "boolean_operation",
                "arguments": {
                    "document_path": "{DOCUMENT}",
                    "operation": "cut",
                    "base_object": base,
                    "tool_object": tool,
                    "result_name": name,
                    "timeout_secs": 30,
                },
            }
        )

    phases["body"].append(
        {
            "tool": "create_document",
            "arguments": {
                "path": "{DOCUMENT}",
                "timeout_secs": 30,
            },
        }
    )
    add("body", "BarrelOuter", 16, 62, [0, 0, 0])
    add("body", "BarrelBore", 13.5, 64, [-1, 0, 0])
    cut("body", "BarrelOuter", "BarrelBore", "BarrelCore")
    add("body", "RearCap", 21, 6, [-6, 0, 0])
    add("body", "FrontCapOuter", 21, 7, [62, 0, 0])
    add("body", "RodPassage", 5.4, 9, [61, 0, 0])
    cut("body", "FrontCapOuter", "RodPassage", "FrontCap")
    add("mechanism", "Piston", 13, 7, [24, 0, 0])
    add("mechanism", "PistonRod", 5, 66, [31, 0, 0])
    for index, x in enumerate((25, 29)):
        phases["mechanism"].append(
            {
                "tool": "add_primitive",
                "arguments": {
                    "document_path": "{DOCUMENT}",
                    "primitive": "torus",
                    "name": "Seal%d" % index,
                    "dimensions": {"radius1": 12.95, "radius2": 0.45},
                    "translation": [x, 0, 0],
                    "rotation_axis": [0, 1, 0],
                    "rotation_degrees": 90,
                    "timeout_secs": 30,
                },
            }
        )
    for index, (y, z) in enumerate(((-13, -13), (-13, 13), (13, -13), (13, 13))):
        add("assembly", "TieRod%d" % index, 1.8, 75, [-6, y, z])
        add("assembly", "TieHead%d" % index, 2.6, 2.4, [69, y, z])
    barrel = "BarrelCore"
    for index, x in enumerate((8, 54)):
        add("assembly", "PortOuter%d" % index, 3.5, 7, [x, 0, 15], degrees=0)
        add("assembly", "PortBore%d" % index, 1.5, 12, [x, 0, 11], degrees=0)
        cut("assembly", "PortOuter%d" % index, "PortBore%d" % index, "AirPort%d" % index)
        name = "BarrelPort0" if index == 0 else "Barrel"
        cut("assembly", barrel, "PortBore%d" % index, name)
        barrel = name
    for name, x in (("Piston", 36), ("PistonRod", 43), ("Seal0", 37), ("Seal1", 41)):
        phases["stroke"].append(
            {
                "tool": "transform_object",
                "arguments": {
                    "document_path": "{DOCUMENT}",
                    "object_name": name,
                    "translation": [x, 0, 0],
                    "rotation_axis": [0, 1, 0],
                    "rotation_degrees": 90,
                    "timeout_secs": 30,
                },
            }
        )
    visible = ["Barrel", "RearCap", "FrontCap", "Piston", "PistonRod", "Seal0", "Seal1"]
    visible += ["TieRod%d" % i for i in range(4)] + ["TieHead%d" % i for i in range(4)]
    visible += ["AirPort0", "AirPort1"]
    colors = []
    for name in visible:
        rgb, opacity = [0.68, 0.72, 0.76], 1.0
        if name == "Barrel":
            rgb, opacity = [0.35, 0.68, 0.80], 0.22
        elif name in ("RearCap", "FrontCap"):
            rgb = [0.16, 0.30, 0.44]
        elif name == "Piston":
            rgb = [0.88, 0.43, 0.15]
        elif name.startswith("Seal"):
            rgb = [0.12, 0.16, 0.17]
        elif name.startswith("AirPort"):
            rgb = [0.72, 0.54, 0.25]
        colors.append({"object_name": name, "rgb": rgb, "opacity": opacity})
    checkpoint_names = {
        "body": ["BarrelCore", "RearCap", "FrontCap"],
        "mechanism": ["BarrelCore", "RearCap", "FrontCap", "Piston", "PistonRod", "Seal0", "Seal1"],
        "assembly": visible,
        "stroke": visible,
    }
    palette = {item["object_name"]: item for item in colors}
    for phase, names in checkpoint_names.items():
        appearances = [
            dict(palette["Barrel" if name == "BarrelCore" else name], object_name=name)
            for name in names
        ]
        output = "{PRESENTATION_" + phase.upper() + "}"
        phases[phase].extend(
            [
                {
                    "tool": "validate_document",
                    "arguments": {"path": "{DOCUMENT}", "timeout_secs": 30},
                },
                {
                    "tool": "inspect_document",
                    "arguments": {"path": "{DOCUMENT}", "timeout_secs": 30},
                },
                {
                    "tool": "save_copy",
                    "arguments": {
                        "source_path": "{DOCUMENT}",
                        "output_path": output,
                        "overwrite": False,
                        "visible_objects": names,
                        "view": "isometric",
                        "appearances": appearances,
                        "frame_margin": 0.12,
                        "timeout_secs": 30,
                    },
                },
                {"tool": "inspect_document", "arguments": {"path": output, "timeout_secs": 30}},
            ]
        )
    return {
        "status": "STORYBOARD_RECIPE_ONLY_NATIVE_UNQUALIFIED",
        "units": "mm",
        "original_geometry": True,
        "phases": phases,
        "final_visible_objects": visible,
        "appearances": colors,
        "presentation": {"view": "isometric", "frame_margin": 0.12},
        "stroke_change_mm": 12,
        "kinematic_joint_or_manufacturing_claim": False,
        "checkpoints": (
            "After each approved phase: validate, inspect, save_copy to a new presentation, "
            "inspect; record actual GUI reopening that file"
        ),
        "notes": (
            "No sketch/dimension or TechDraw endpoint is claimed. Property tree shows real "
            "primitive dimensions and placements. Torus seals and Boolean results require "
            "native qualification before recording."
        ),
    }


if __name__ == "__main__":
    print(json.dumps(recipe(), indent=2))
