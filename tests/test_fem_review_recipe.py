"""The fem-review recipe contract: a verdict that cannot hide its assumptions.

The bug class under test is not a wrong number. ``run_fem_analysis`` already
pins the numbers against a closed-form cantilever. What it does not pin is the
layer above them, and that layer is where a FEM result does its damage: it
converges, it looks precise, and a reader supplies the missing premises
themselves. "Max von Mises is 240 MPa" is a fact; "this part is fine" is a
conclusion that needs a yield strength, a safety target, a location, and a
statement of what the model cannot see.

So every acceptance rule in the issue is asserted here as a schema that fails,
not as prose in SKILL.md. The negative cases are the point of the file: a
contract that has only ever been satisfied has not been tested.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from dcc_mcp_core import validate_skill
from dcc_mcp_core.recipes import load_recipe_pack, validate_recipe_inputs

SKILLS = Path(__file__).parents[1] / "src" / "dcc_mcp_freecad" / "skills"
SKILL_DIR = SKILLS / "freecad-fem-review"
RECIPES = SKILL_DIR / "RECIPES.yaml"


def _recipe():
    pack = load_recipe_pack(str(RECIPES), skill_name="freecad-fem-review")
    assert [item.name for item in pack] == ["fem-review"], "the recipe pack must load one recipe"
    return pack[0].to_dict()


RECIPE = _recipe()
OUTPUT_CONTRACT = RECIPE["output_contract"]


def _validate(review):
    """Validate a review against the output contract the way a caller would."""
    return validate_recipe_inputs({"inputs_schema": OUTPUT_CONTRACT}, review)


def _location(**overrides):
    location = {
        "object_name": "Beam",
        "reference": "Beam:Face3",
        "kind": "geometric_hot_spot",
        "von_mises": {"value": 60.0, "unit": "MPa"},
        "note": "Root of the cantilever, where bending moment peaks.",
    }
    location.update(overrides)
    return location


def _action(**overrides):
    action = {
        "action": "Add a 3 mm fillet at the root",
        "rationale": "The peak is the root corner; the free end carries no moment.",
        "target": "Beam:Face3",
    }
    action.update(overrides)
    return action


ASSUMPTIONS = [
    {
        "topic": "constraint_idealisation",
        "statement": "The fixed face is perfectly rigid; a real mounting flexes.",
    },
    {
        "topic": "material_provenance",
        "statement": (
            "Yield strength 250 MPa from the built-in generic table for "
            "DccMcpStructuralSteel. Confirm against the mill certificate."
        ),
    },
    {
        "topic": "mesh_convergence",
        "statement": "Unchecked: solved at a single 2 mm mesh size.",
    },
]

GOOD = {
    "verdict": "satisfied",
    "safety_factor": 2.0833333333333335,
    "allowable_stress": {
        "value": 125.0,
        "unit": "MPa",
        "source": "yield 250 MPa / target safety factor 2.0",
    },
    "critical_locations": [_location()],
    "next_actions": [_action()],
    "assumptions": copy.deepcopy(ASSUMPTIONS),
    "deflection_check": {
        "max_displacement": {"value": 0.190476, "unit": "mm"},
        "verdict": "no_limit_given",
    },
    "evidence": {
        "max_von_mises": {"value": 60.0, "unit": "MPa"},
        "source_analysis": "Analysis",
        "node_count": 4000,
    },
}


# ── The skill itself ───────────────────────────────────────────────────────


def test_skill_validates_cleanly():
    report = validate_skill(str(SKILL_DIR))
    errors = [issue.message for issue in report.issues if issue.severity == "error"]
    assert errors == []


def test_the_review_skill_adds_no_typed_tool():
    """The interpretation layer is orchestration only.

    A recipe that quietly grew a tool of its own would be a domain tool wearing
    a recipe's clothes, and the whole reason this layer is not a tool is that
    its output is judgement rather than a deterministic operation.
    """
    assert not (SKILL_DIR / "tools.yaml").exists()
    assert not (SKILL_DIR / "scripts").exists()


def test_recipe_is_discoverable_and_carries_its_contract():
    assert RECIPE["dcc"] == "freecad"
    assert RECIPE["description"]
    assert OUTPUT_CONTRACT["type"] == "object"
    assert OUTPUT_CONTRACT["required"]


def test_frontmatter_declares_the_recipe_and_its_dependency():
    frontmatter = yaml.safe_load(
        (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    )
    dcc_mcp = frontmatter["metadata"]["dcc-mcp"]
    assert dcc_mcp["recipes"] == "RECIPES.yaml"
    assert dcc_mcp["depends"] == ["freecad-analysis"]


# ── Acceptance 5: the marketplace three-field contract ─────────────────────


def test_marketplace_contract_fields_are_declared():
    frontmatter = yaml.safe_load(
        (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    )
    dcc_mcp = frontmatter["metadata"]["dcc-mcp"]

    prompts = dcc_mcp["examplePrompts"]
    assert len(prompts) >= 1
    for prompt in prompts:
        assert len(prompt) >= 8, "an example prompt shorter than this is not a request"
        assert "run_fem_analysis" not in prompt, "prompts are user language, not tool names"

    recovery = dcc_mcp["recovery"]
    assert len(recovery) >= 1
    for entry in recovery:
        assert entry["next"], "a recovery step with no next action is a dead end"
        assert entry["note"]

    assert dcc_mcp["undo"] == "none", "the recipe is read-only; see SKILL.md for why that is safe"


def test_undo_none_is_justified_in_the_body():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "undo: none" in body
    assert "nothing" in body.lower(), "undo: none must say what there is to roll back"


# ── Inputs ────────────────────────────────────────────────────────────────


def test_a_solve_payload_is_accepted():
    inputs = {
        "fem_result": {
            "max_von_mises": {"value": 60.0, "unit": "MPa"},
            "max_displacement": {"value": 0.190476, "unit": "mm"},
            "axis_displacement": {"value": -0.190476, "unit": "mm"},
            "material": {"name": "DccMcpStructuralSteel"},
            "fixed_faces": ["Beam:Face3"],
            "node_count": 4000,
        }
    }
    assert validate_recipe_inputs(RECIPE, inputs) == []


def test_a_review_with_no_solve_to_read_is_refused():
    assert validate_recipe_inputs(RECIPE, {})


def test_a_bare_float_stress_is_refused():
    """The unit discipline does not stop at the analysis skill's boundary.

    ``run_fem_analysis`` refuses a bare float on the way in. A review that
    accepted one on the way through would reintroduce the same error at the
    point where it is hardest to spot -- after the numbers look trustworthy.
    """
    errors = validate_recipe_inputs(
        RECIPE,
        {
            "fem_result": {
                "max_von_mises": 60.0,
                "max_displacement": {"value": 0.19, "unit": "mm"},
            }
        },
    )
    assert errors


def test_a_stress_with_no_unit_is_refused():
    errors = validate_recipe_inputs(
        RECIPE,
        {
            "fem_result": {
                "max_von_mises": {"value": 60.0},
                "max_displacement": {"value": 0.19, "unit": "mm"},
            }
        },
    )
    assert errors


def test_unknown_input_fields_are_refused():
    errors = validate_recipe_inputs(
        RECIPE,
        {
            "fem_result": {
                "max_von_mises": {"value": 60.0, "unit": "MPa"},
                "max_displacement": {"value": 0.19, "unit": "mm"},
            },
            "vibes": "looks fine to me",
        },
    )
    assert errors


@pytest.mark.parametrize("factor", [0, -1, -2.5])
def test_a_non_positive_safety_target_is_refused(factor):
    errors = validate_recipe_inputs(
        RECIPE,
        {
            "fem_result": {
                "max_von_mises": {"value": 60.0, "unit": "MPa"},
                "max_displacement": {"value": 0.19, "unit": "mm"},
            },
            "target_safety_factor": factor,
        },
    )
    assert errors


# ── A conforming review, end to end ───────────────────────────────────────


def test_the_cantilever_review_validates():
    assert _validate(GOOD) == []


def test_the_worked_example_is_the_verified_cantilever():
    """Acceptance 4: the example is the case the solver is verified against.

    A worked example built on an invented solve would be decoration. The
    cantilever has a closed-form answer both lanes of CI assert, so an example
    built on it can actually be checked rather than merely read.
    """
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "cantilever" in body.lower()
    for token in ("0.190476", "60.0 MPa", "docs/validation/fem-cantilever.md"):
        assert token in body, token


def test_a_violated_verdict_validates_when_the_stress_exceeds_allowable():
    review = copy.deepcopy(GOOD)
    review["verdict"] = "violated"
    review["safety_factor"] = 0.8
    review["critical_locations"] = [_location(von_mises={"value": 156.25, "unit": "MPa"})]
    assert _validate(review) == []


# ── Acceptance 1: assumptions are a hard gate ─────────────────────────────


def test_a_review_with_no_assumption_block_is_rejected():
    review = copy.deepcopy(GOOD)
    review.pop("assumptions")
    assert _validate(review)


def test_a_review_with_an_empty_assumption_list_is_rejected():
    review = copy.deepcopy(GOOD)
    review["assumptions"] = []
    assert _validate(review)


def test_a_review_with_fewer_than_three_assumptions_is_rejected():
    review = copy.deepcopy(GOOD)
    review["assumptions"] = copy.deepcopy(ASSUMPTIONS)[:2]
    assert _validate(review)


@pytest.mark.parametrize(
    "topic",
    ["constraint_idealisation", "material_provenance", "mesh_convergence"],
)
def test_each_mandatory_assumption_topic_is_individually_required(topic):
    """Dropping any one of the three mandatory topics fails the review.

    Asserted one topic at a time so a contract that only enforces the count,
    and not the content, is caught by the case that removes the topic it does
    not check.
    """
    review = copy.deepcopy(GOOD)
    review["assumptions"] = [item for item in copy.deepcopy(ASSUMPTIONS) if item["topic"] != topic]
    review["assumptions"].append(
        {"topic": "model_class", "statement": "Linear static, small displacement."}
    )
    errors = _validate(review)
    assert errors, "removing the %s assumption was accepted" % topic


def test_an_assumption_with_no_statement_is_rejected():
    review = copy.deepcopy(GOOD)
    review["assumptions"][0].pop("statement")
    assert _validate(review)


def test_an_assumption_with_an_unknown_topic_is_rejected():
    review = copy.deepcopy(GOOD)
    review["assumptions"][0]["topic"] = "it_was_fine_on_my_machine"
    assert _validate(review)


# ── Acceptance 2: a location is named, never just a global maximum ─────────


def test_a_review_with_no_location_is_rejected():
    review = copy.deepcopy(GOOD)
    review.pop("critical_locations")
    assert _validate(review)


def test_a_global_maximum_with_no_location_list_is_rejected():
    """The failure the criterion exists to prevent.

    A verdict carrying only the peak number tells the user the part is stressed
    and not where, which is the one thing they cannot get from the solve and
    the only thing they need from the review.
    """
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = []
    errors = _validate(review)
    assert errors
    assert any("critical_locations" in error for error in errors)


def test_a_location_with_no_sub_element_reference_is_rejected():
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = [_location(reference=None)]
    review["critical_locations"][0].pop("reference")
    assert _validate(review)


def test_a_location_with_a_free_text_reference_is_rejected():
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = [_location(reference="near the root, sort of")]
    assert _validate(review)


def test_a_location_with_a_malformed_reference_is_rejected():
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = [_location(reference="Beam:Face")]
    assert _validate(review)


def test_a_location_with_no_kind_is_rejected():
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = [_location(kind=None)]
    review["critical_locations"][0].pop("kind")
    assert _validate(review)


def test_a_boundary_artefact_is_a_distinguishable_kind():
    """Nodal stress at a fully restrained face is the solver's least reliable
    number. It has to be reportable as exactly that, so it is not read as a
    design finding.
    """
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = [_location(kind="boundary_condition_artefact")]
    assert _validate(review) == []


def test_a_location_that_names_no_object_is_rejected():
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = [_location(object_name="")]
    assert _validate(review)


# ── Acceptance 3: material provenance ─────────────────────────────────────


def test_allowable_stress_must_name_its_source():
    review = copy.deepcopy(GOOD)
    review["allowable_stress"].pop("source")
    assert _validate(review)


def test_an_empty_provenance_string_is_not_a_source():
    review = copy.deepcopy(GOOD)
    review["allowable_stress"]["source"] = ""
    assert _validate(review)


def test_the_built_in_material_table_declares_its_own_uncertainty():
    """A default yield strength is a guess with a number attached.

    It is fine to default; it is not fine to default silently, because the
    reader cannot tell a datasheet value from a textbook minimum.
    """
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "DccMcpStructuralSteel" in body
    assert "generic" in body.lower() or "textbook" in body.lower()
    assert "certificate" in body.lower() or "datasheet" in body.lower()


def test_the_built_in_default_matches_the_analysis_default():
    """A review of a default solve must not invent a different material.

    run_fem_analysis solves with structural steel when no material is passed.
    A review that assumed some other default would report a safety factor the
    solve never computed.
    """
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "250 MPa" in body
    assert "210000" in body


def test_a_user_supplied_yield_strength_is_accepted_and_sourced():
    inputs = {
        "fem_result": {
            "max_von_mises": {"value": 60.0, "unit": "MPa"},
            "max_displacement": {"value": 0.19, "unit": "mm"},
        },
        "yield_strength": {
            "value": 350.0,
            "unit": "MPa",
            "source": "mill certificate EN 10204 3.1, heat 4471",
        },
    }
    assert validate_recipe_inputs(RECIPE, inputs) == []


def test_a_yield_strength_with_no_unit_is_refused():
    errors = validate_recipe_inputs(
        RECIPE,
        {
            "fem_result": {
                "max_von_mises": {"value": 60.0, "unit": "MPa"},
                "max_displacement": {"value": 0.19, "unit": "mm"},
            },
            "yield_strength": {"value": 350.0},
        },
    )
    assert errors


# ── Verdict and safety factor ─────────────────────────────────────────────


@pytest.mark.parametrize("verdict", ["satisfied", "marginal", "violated"])
def test_each_verdict_band_is_valid(verdict):
    review = copy.deepcopy(GOOD)
    review["verdict"] = verdict
    assert _validate(review) == []


def test_an_off_menu_verdict_is_rejected():
    review = copy.deepcopy(GOOD)
    review["verdict"] = "probably fine"
    assert _validate(review)


def test_a_non_positive_safety_factor_is_rejected():
    review = copy.deepcopy(GOOD)
    review["safety_factor"] = 0.0
    assert _validate(review)


def test_a_review_with_no_safety_factor_is_rejected():
    review = copy.deepcopy(GOOD)
    review.pop("safety_factor")
    assert _validate(review)


def test_next_actions_cannot_be_empty():
    review = copy.deepcopy(GOOD)
    review["next_actions"] = []
    assert _validate(review)


def test_a_next_action_with_no_rationale_is_rejected():
    review = copy.deepcopy(GOOD)
    review["next_actions"] = [_action(rationale=None)]
    review["next_actions"][0].pop("rationale")
    assert _validate(review)


def test_the_contract_refuses_unknown_output_fields():
    review = copy.deepcopy(GOOD)
    review["trust_me"] = True
    assert _validate(review)


# ── Evidence and deflection ───────────────────────────────────────────────


def test_the_evidence_block_must_tie_back_to_the_solve():
    review = copy.deepcopy(GOOD)
    review.pop("evidence")
    assert _validate(review)


def test_evidence_without_a_source_analysis_is_rejected():
    review = copy.deepcopy(GOOD)
    review["evidence"].pop("source_analysis")
    assert _validate(review)


def test_deflection_is_judged_separately_from_strength():
    """A part can be strong enough and still too floppy.

    Stiffness and strength are different limits with different remedies, so a
    review that reported only the stress verdict would answer a question the
    user did not ask.
    """
    review = copy.deepcopy(GOOD)
    review["deflection_check"] = {
        "max_displacement": {"value": 12.0, "unit": "mm"},
        "limit": {"value": 1.0, "unit": "mm"},
        "verdict": "exceeds_limit",
    }
    assert _validate(review) == []


def test_a_deflection_check_with_no_verdict_is_rejected():
    review = copy.deepcopy(GOOD)
    review["deflection_check"].pop("verdict")
    assert _validate(review)


# ── The contract itself ───────────────────────────────────────────────────


def test_every_step_tool_is_routed():
    """An unrouted step is a step nobody can execute.

    Same lock as the tool catalog: a declaration that advertises something the
    runtime cannot reach fails silently at the worst possible moment.
    """
    for step in RECIPE["steps"]:
        routing = step.get("tool_routing") or {}
        assert routing, "step %r has no tool_routing" % step.get("id")
        assert routing.get("freecad") == step["tool"], step.get("id")


def test_every_placeholder_is_a_declared_input():
    """A placeholder with no input to fill it materialises as a literal."""
    import re

    declared = set((RECIPE["inputs_schema"].get("properties") or {}).keys())
    for step in RECIPE["steps"]:
        for value in (step.get("inputs") or {}).values():
            if not isinstance(value, str):
                continue
            for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value):
                assert name in declared, "step %r references undeclared %r" % (
                    step.get("id"),
                    name,
                )


def test_the_output_contract_can_fail():
    """A contract that cannot fail is documentation.

    This is the meta-lock on the whole file: if deleting the required block
    still validates, every other assertion here is decorative.
    """
    review = copy.deepcopy(GOOD)
    review.pop("assumptions")
    review.pop("critical_locations")
    errors = _validate(review)
    assert len(errors) >= 2
