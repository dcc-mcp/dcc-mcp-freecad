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
import re
import warnings
from pathlib import Path

import pytest
import yaml
from dcc_mcp_core import validate_skill
from dcc_mcp_core.recipes import load_recipe_pack, validate_recipe_inputs
from jsonschema import Draft202012Validator

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

# The cantilever: 60 MPa peak against a 250 MPa yield. The safety factor is
# 250/60 = 4.167 -- the margin against yield, not 125/60 = 2.083. The latter
# divides by the target twice and is the error test_arithmetic_* exists to kill.
YIELD_MPA = 250.0
TARGET_SF = 2.0
PEAK_MPA = 60.0
SAFETY_FACTOR = YIELD_MPA / PEAK_MPA
ALLOWABLE_MPA = YIELD_MPA / TARGET_SF

GOOD = {
    "verdict": "satisfied",
    "safety_factor": SAFETY_FACTOR,
    "verdict_basis": "yield_safety_factor",
    "yield_strength": {
        "value": YIELD_MPA,
        "unit": "MPa",
        "source": "built-in generic table for DccMcpStructuralSteel",
    },
    "allowable_stress": {
        "value": ALLOWABLE_MPA,
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
        "max_von_mises": {"value": PEAK_MPA, "unit": "MPa"},
        "source_analysis": "Analysis",
        "node_count": 4000,
    },
}


def _utilisation_review():
    """A review judged against a code allowable, with no yield strength."""
    review = copy.deepcopy(GOOD)
    review.pop("safety_factor")
    review.pop("yield_strength")
    review["verdict_basis"] = "allowable_utilisation"
    review["utilisation"] = PEAK_MPA / ALLOWABLE_MPA
    review["allowable_stress"] = {
        "value": ALLOWABLE_MPA,
        "unit": "MPa",
        "source": "ASME VIII Div.1 allowable for the design temperature",
    }
    return review


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
    review["safety_factor"] = YIELD_MPA / 156.25
    review["critical_locations"] = [_location(von_mises={"value": 156.25, "unit": "MPa"})]
    review["evidence"]["max_von_mises"] = {"value": 156.25, "unit": "MPa"}
    assert _validate(review) == []
    assert review["safety_factor"] < TARGET_SF


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


def test_a_yield_basis_review_with_no_safety_factor_is_rejected():
    review = copy.deepcopy(GOOD)
    review.pop("safety_factor")
    assert _validate(review)


def test_a_yield_basis_review_must_echo_the_yield_it_used():
    """A safety factor with no visible dividend is not auditable.

    If the yield is not in the output, the reader cannot re-derive 4.167 from
    250 and 60, and has to take the verdict on trust -- which is the exact
    failure mode the assumption block is meant to prevent.
    """
    review = copy.deepcopy(GOOD)
    review.pop("yield_strength")
    assert _validate(review)


def test_an_allowable_basis_review_must_report_utilisation():
    """No yield means no safety factor, so one must not be invented."""
    review = _utilisation_review()
    review.pop("utilisation")
    assert _validate(review)


def test_an_allowable_basis_review_validates():
    assert _validate(_utilisation_review()) == []


def test_a_yield_basis_review_may_not_report_utilisation_as_its_basis():
    review = copy.deepcopy(GOOD)
    review["verdict_basis"] = "allowable_utilisation"
    assert _validate(review), "switching basis without supplying utilisation must fail"


@pytest.mark.parametrize("basis", ["yield_safety_factor", "yield-over-target", ""])
def test_an_off_menu_verdict_basis_is_rejected(basis):
    review = copy.deepcopy(GOOD)
    review["verdict_basis"] = basis
    if basis == "yield_safety_factor":
        assert _validate(review) == []
    else:
        assert _validate(review)


# ── Arithmetic consistency ────────────────────────────────────────────────
#
# The bug that produced this section: the safety factor was defined as
# allowable/max_stress, but the allowable already had the target divided into
# it. That demands SF >= target^2, so a part with 4.17x margin was reported at
# 2.08 and, at a target of 3, as failing outright. Every test in this section
# is a mutation arm the original suite survived.


def test_the_safety_factor_is_the_yield_divided_by_the_peak_stress():
    """The headline arithmetic check.

    The fixture is the verified cantilever: 60 MPa peak, 250 MPa yield. The
    safety factor must be 4.167, not 2.083 -- and this asserts the relationship
    rather than the literal, so it cannot be satisfied by hard-coding.
    """
    peak = GOOD["evidence"]["max_von_mises"]["value"]
    yield_mpa = GOOD["yield_strength"]["value"]
    assert GOOD["safety_factor"] == pytest.approx(yield_mpa / peak)
    assert GOOD["safety_factor"] == pytest.approx(4.166666666666667)


def test_the_safety_factor_is_not_the_allowable_divided_by_the_peak():
    """The wrong formula, asserted against explicitly.

    If the contract ever drifts back to allowable/max_stress, this fails on the
    number itself instead of waiting for a reader to notice the verdict moved.
    """
    peak = GOOD["evidence"]["max_von_mises"]["value"]
    assert GOOD["safety_factor"] != pytest.approx(GOOD["allowable_stress"]["value"] / peak)


def test_the_allowable_is_the_yield_divided_by_the_target():
    assert GOOD["allowable_stress"]["value"] == pytest.approx(YIELD_MPA / TARGET_SF)


@pytest.mark.parametrize("target", [1.5, 2.0, 3.0, 5.0])
def test_the_verdict_does_not_flip_as_the_target_changes(target):
    """The symptom that made the P1 a P1.

    Under the buggy formula the verdict flipped from satisfied to violated as
    the target rose, because the reported factor collapsed toward zero while
    the real margin stayed at 4.167. The verdict must be a function of the
    margin and the target, never of the target twice.
    """
    peak = GOOD["evidence"]["max_von_mises"]["value"]
    safety_factor = YIELD_MPA / peak
    allowable = YIELD_MPA / target
    # The stress check and the safety-factor check must always agree.
    stress_ok = peak <= allowable
    factor_ok = safety_factor >= target
    assert stress_ok == factor_ok, "target=%s: stress check and factor check disagree" % target


def test_a_violated_review_is_arithmetically_consistent():
    """A violated verdict must actually be below the target, not merely labelled."""
    review = copy.deepcopy(GOOD)
    review["verdict"] = "violated"
    stressed = 208.0
    review["safety_factor"] = YIELD_MPA / stressed  # 1.202, below the 2.0 target
    review["critical_locations"] = [_location(von_mises={"value": stressed, "unit": "MPa"})]
    review["evidence"]["max_von_mises"] = {"value": stressed, "unit": "MPa"}
    assert _validate(review) == []
    assert review["safety_factor"] < TARGET_SF
    assert review["safety_factor"] == pytest.approx(YIELD_MPA / stressed)


def test_the_bands_are_defined_in_the_skill_body():
    """'Never rounded into a different band' is unenforceable without numbers.

    The original contract had an enum and no thresholds, so the rule was prose.
    Each band's defining comparison is asserted individually -- checking only
    that the text mentions 1.1 would survive deleting any single band's rule.
    """
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    # The band table: one row per verdict, each naming its own comparison.
    rows = re.findall(r"^\|\s*`(\w+)`\s*\|\s*`([^`]+)`\s*\|", body, re.MULTILINE)
    bands = dict(rows)
    assert set(bands) >= {"satisfied", "marginal", "violated"}, (
        "every verdict needs a band rule, found %s" % sorted(bands)
    )
    assert bands["violated"] == "safety_factor < target"
    assert bands["marginal"] == "target <= safety_factor < target * 1.1"
    assert bands["satisfied"] == "safety_factor >= target * 1.1"
    # The marginal band must have real width, or it is a rounding artefact.
    assert "10%" in body, "the marginal width is not stated"


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


def _declared_tools():
    """Every tool the adapter actually ships, as {skill.tool: schema}.

    Built from the same tree the server loads, so a routing entry that names a
    skill or tool which does not exist is caught here rather than at runtime.
    """
    tools = {}
    for skill_dir in sorted(SKILLS.iterdir()):
        manifest = skill_dir / "tools.yaml"
        if not manifest.is_file():
            continue
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        for tool in payload.get("tools") or ():
            tools["%s.%s" % (skill_dir.name, tool["name"])] = tool
    assert tools, "no tools were collected; the routing lock ran vacuously"
    return tools


def test_every_step_tool_exists_in_the_adapter():
    """A step pointing at a tool that does not exist is a step nobody can run.

    The previous form only checked that tool_routing agreed with tool, which a
    typo satisfies perfectly -- `freecad-modeling.inspect_document` passed while
    the tool actually lives in freecad-session. This resolves the address.
    """
    tools = _declared_tools()
    for step in RECIPE["steps"]:
        address = step.get("tool")
        if step.get("verb") == "report":
            # The report step is this recipe's own output, not a host tool.
            continue
        assert address in tools, "step %r names %r, which the adapter does not ship" % (
            step.get("id"),
            address,
        )
        routing = step.get("tool_routing") or {}
        assert routing, "step %r has no tool_routing" % step.get("id")
        assert routing.get("freecad") == address, step.get("id")


def test_step_inputs_match_the_schema_of_the_tool_they_call():
    """The right tool with the wrong argument names is just as unexecutable.

    `inspect_document` takes `path` and sets additionalProperties: false, so
    passing `document_path` is rejected outright. Names have to be checked
    against the tool's own schema, not against whatever the step author typed.
    """
    tools = _declared_tools()
    problems = []
    for step in RECIPE["steps"]:
        address = step.get("tool")
        tool = tools.get(address)
        if tool is None:
            continue
        properties = (tool.get("input_schema") or {}).get("properties") or {}
        for name in step.get("inputs") or ():
            if name not in properties:
                problems.append(
                    "step %r passes %r to %s, which declares %s"
                    % (step.get("id"), name, address, sorted(properties))
                )
    assert problems == []


def test_every_placeholder_resolves_to_a_declared_path():
    """A placeholder with no input to fill it materialises as a literal.

    The pattern must accept dotted paths: every placeholder in this recipe is
    one, and a pattern without the dot matched nothing at all, so the previous
    form of this test validated zero placeholders.
    """
    schemas = {"fem_result": RECIPE["inputs_schema"]["properties"]["fem_result"]}
    schemas.update(
        {name: schema for name, schema in (RECIPE["inputs_schema"]["properties"] or {}).items()}
    )
    problems = []
    for step in RECIPE["steps"]:
        for key in ("inputs",):
            for value in (step.get(key) or {}).values():
                if not isinstance(value, str):
                    continue
                for path in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}", value):
                    if not _path_exists(path, schemas):
                        problems.append(
                            "step %r (%.16s) references undeclared %r" % (step.get("id"), key, path)
                        )
    assert problems == []


def _placeholder_schemas():
    """{placeholder root: schema} for every input the recipe declares."""
    return dict(RECIPE["inputs_schema"]["properties"])


def _declared_schema(path):
    """The schema at a dotted placeholder path, or None when undeclared.

    An indexed reference such as ``${fem_result.fixed_faces[0]}`` resolves to
    the item schema of its root array: indexing a declared list yields an
    element, not the array.
    """
    indexed = re.findall(r"\[\d+\]", path)
    segments = re.sub(r"\[\d+\]", "", path).split(".")
    node = _placeholder_schemas().get(segments[0])
    if node is None:
        return None
    for segment in segments[1:]:
        if not isinstance(node, dict):
            return None
        node = (node.get("properties") or {}).get(segment)
        if node is None:
            return None
    for _index in indexed:
        if not isinstance(node, dict):
            return None
        node = node.get("items")
        if node is None:
            return None
    return node


def _is_shape_compatible(source_schema, target_schema):
    """True when a value shaped like ``source_schema`` can satisfy ``target``.

    Compares the two leaf schemas field by field: a source value is compatible
    when every constraint the target states is met by a constraint the source
    states. A source may be stricter than the target and still be compatible;
    it may not be looser.
    """
    if not isinstance(source_schema, dict) or not isinstance(target_schema, dict):
        return True

    source_type = source_schema.get("type")
    target_type = target_schema.get("type")
    if source_type is not None and target_type is not None and source_type != target_type:
        return False

    for key in ("pattern", "format", "const"):
        if key in target_schema and source_schema.get(key) != target_schema[key]:
            return False

    if "enum" in target_schema:
        source_enum = source_schema.get("enum")
        if source_enum is None:
            return False
        if not set(source_enum) <= set(target_schema["enum"]):
            return False

    for key, worse in (("minLength", min), ("minimum", min), ("minItems", min)):
        if key in target_schema and source_schema.get(key) is not None:
            if worse(source_schema[key], target_schema[key]) != target_schema[key]:
                return False
    for key, worse in (("maxLength", max), ("maximum", max), ("maxItems", max)):
        if key in target_schema and source_schema.get(key) is not None:
            if worse(source_schema[key], target_schema[key]) != target_schema[key]:
                return False

    if "items" in target_schema and "items" in source_schema:
        return _is_shape_compatible(source_schema["items"], target_schema["items"])
    if "items" in target_schema and source_schema.get("type") == "array":
        return False
    return True


def _placeholder_assignments(value):
    """Every ``${...}`` reference in a step input value, with literal text kept.

    A value built from literal text plus a placeholder -- ``"face ${x}"`` -- is
    checked against the placeholder's own shape, because a literal prefix
    cannot make an incompatible value compatible.
    """
    return re.findall(r"\$\{([^}]*)\}", value) if isinstance(value, str) else []


def test_step_input_values_match_the_shape_the_tool_accepts():
    """A declared path of the wrong shape fails inside the tool, not here.

    ``test_every_placeholder_resolves_to_a_declared_path`` proves a
    placeholder names something that exists. It does not prove the thing it
    names is shaped the way the receiving tool demands, and that gap is not
    hypothetical: this recipe's ``anchor`` step once passed
    ``${fem_result.fixed_faces[0]}`` to ``list_faces.object_name``. The path
    resolves, the name is right, and the value -- ``"Beam:Face3"``, a face
    reference -- is rejected by that step's own tool, whose ``object_name``
    pattern admits bare object names only. Every test in this file stayed
    green while that was true.

    The check runs each placeholder's declared schema against the tool field's
    schema, so it is the shapes that must agree, not the names.
    """
    tools = _declared_tools()
    problems = []
    for step in RECIPE["steps"]:
        address = step.get("tool")
        tool = tools.get(address)
        if tool is None:
            continue
        properties = (tool.get("input_schema") or {}).get("properties") or {}
        for name, value in (step.get("inputs") or {}).items():
            target_schema = properties.get(name)
            if target_schema is None:
                continue
            for path in _placeholder_assignments(value):
                source_schema = _declared_schema(path)
                if source_schema is None:
                    continue  # undeclared path; the existence check reports it
                if not _is_shape_compatible(source_schema, target_schema):
                    problems.append(
                        "step %r feeds %s ${%s} (shape %s) to %s.%s, "
                        "which accepts %s"
                        % (
                            step.get("id"),
                            name,
                            path,
                            _describe(source_schema),
                            address.split(".")[0],
                            name,
                            _describe(target_schema),
                        )
                    )
    assert problems == []


def _describe(schema):
    """A one-line shape summary for a failure message."""
    if not isinstance(schema, dict):
        return repr(schema)
    parts = []
    for key in ("type", "pattern", "enum", "format", "items"):
        if key in schema:
            parts.append("%s=%r" % (key, schema[key]))
    return "{%s}" % ", ".join(parts) if parts else repr(schema)


def test_the_shape_guard_can_fail():
    """The meta-lock on the shape check.

    A compatibility predicate that returns True for everything is a decoration
    that no mutation can ever trip. The anti-pattern this section was written
    to catch -- a face reference fed to an object-name field -- is asserted to
    be rejected here, so the guard cannot rot into a no-op.
    """
    face_reference = {
        "type": "string",
        "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,63}:(Face|Edge|Vertex)[0-9]+$",
    }
    object_name = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,63}$",
    }
    assert not _is_shape_compatible(face_reference, object_name)
    # ...and the direction the recipe actually uses still passes.
    assert _is_shape_compatible(object_name, object_name)
    # A looser source is not accepted either: no pattern means no guarantee.
    assert not _is_shape_compatible({"type": "string"}, object_name)


def test_a_step_input_value_is_valid_against_the_tool_schema_itself():
    """The same agreement, checked by the validator rather than by hand.

    ``_is_shape_compatible`` compares schemas; this validates a concrete value
    drawn from the declared shape against the tool's own ``input_schema`` using
    the validator the server uses. One is a comparison of two schemas, the
    other is an end-to-end check, and a bug in the hand-written comparison does
    not automatically hide the second.
    """
    tools = _declared_tools()
    checked = 0
    for step in RECIPE["steps"]:
        tool = tools.get(step.get("tool"))
        if tool is None:
            continue
        schema = tool.get("input_schema") or {}
        for name, value in (step.get("inputs") or {}).items():
            if not isinstance(value, str) or "${" not in value:
                continue
            for path in _placeholder_assignments(value):
                source_schema = _declared_schema(path)
                if not isinstance(source_schema, dict):
                    continue
                candidate = _example_value(source_schema)
                if candidate is _NO_EXAMPLE:
                    continue
                field_schema = (schema.get("properties") or {}).get(name)
                if not isinstance(field_schema, dict):
                    continue
                errors = list(Draft202012Validator(field_schema).iter_errors(candidate))
                checked += 1
                assert not errors, (
                    "step %r would send %s=%r to %s; the tool schema rejects it: %s"
                    % (
                        step.get("id"),
                        name,
                        candidate,
                        step.get("tool"),
                        "; ".join(error.message for error in errors),
                    )
                )
    assert checked >= 1, "no placeholder input was checked; the test ran vacuously"


_NO_EXAMPLE = object()


def _example_value(schema):
    """A concrete value of the shape ``schema`` describes, or _NO_EXAMPLE."""
    if not isinstance(schema, dict):
        return _NO_EXAMPLE
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "string":
        pattern = schema.get("pattern")
        if pattern:
            return _string_matching(pattern, schema)
        return "x"
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "number":
        return float(schema.get("minimum", 0)) or 1.0
    if kind == "boolean":
        return False
    if kind == "array":
        item = _example_value(schema.get("items", {}))
        return [] if item is _NO_EXAMPLE else [item]
    return _NO_EXAMPLE


def _string_matching(pattern, schema):
    """A string satisfying ``pattern`` and the length bounds, or _NO_EXAMPLE.

    Generated by walking the compiled pattern tree and taking each mandatory
    token's cheapest filler: the first character of a character class, the
    lower bound of a repetition, the first branch of an alternation. Anchors
    contribute nothing and are skipped.

    Two earlier attempts are worth recording, because both failed on the exact
    pattern this section exists to catch. Appending ``.*`` to the pattern
    cannot help: the ``:`` in the face-reference pattern is mandatory and sits
    at a fixed offset, so no wildcard absorbs a prefix that has not reached it.
    Relaxing the tail to optional -- ``(?:...)?`` -- accepts dead ends, since
    each relaxation is checked independently and the one that lets ``a`` repeat
    forever is never the one that completes. A generator that silently gives up
    is worse than none: it makes the guard pass without checking anything.
    """
    with warnings.catch_warnings():
        # sre_parse is deprecated from 3.11 but remains the only stdlib way to
        # read a compiled pattern's structure back out; re._parser is private
        # and moves between versions.
        warnings.simplefilter("ignore", DeprecationWarning)
        import sre_parse

        try:
            parsed = sre_parse.parse(pattern)
        except Exception:
            return _NO_EXAMPLE

    try:
        value = _generate_match(parsed)
    except _UnsupportedPattern:
        return _NO_EXAMPLE

    if not re.fullmatch(pattern, value):
        return _NO_EXAMPLE

    min_length = schema.get("minLength", 0)
    max_length = schema.get("maxLength")
    while len(value) < min_length:
        value += "x"
        if not re.fullmatch(pattern, value):
            return _NO_EXAMPLE
    if max_length is not None and len(value) > max_length:
        return _NO_EXAMPLE
    return value


def _generate_match(subpattern):
    """The cheapest string the parsed ``subpattern`` matches."""
    parts = []
    for opcode, argument in subpattern:
        name = str(opcode).split(".")[-1].lower()
        if name == "literal":
            parts.append(chr(argument))
        elif name == "at":
            continue  # ^ and $ constrain position, not content
        elif name == "in":
            parts.append(_generate_class(argument))
        elif name in ("max_repeat", "min_repeat"):
            lower, _upper, inner = argument[0], argument[1], argument[2]
            parts.append(_generate_match(inner) * max(lower, 0))
        elif name == "subpattern":
            parts.append(_generate_match(argument[3]))
        elif name == "branch":
            # Any branch is a valid match; the first keeps the example short.
            parts.append(_generate_match(argument[1][0]))
        elif name == "any":
            parts.append("a")
        elif name in ("category", "category_digit", "category_not_digit"):
            parts.append("a" if "not" not in name else "0")
        else:
            raise _UnsupportedPattern(name)
    return "".join(parts)


def _generate_class(argument):
    """One character from a character-class opcode."""
    for item in argument:
        if isinstance(item, tuple) and len(item) == 2:
            name = str(item[0]).split(".")[-1].lower()
            value = item[1]
            if name == "literal":
                return chr(value)
            if name == "range":
                return chr(value[0])
            if name == "category":
                return "a"
            if name == "branch":
                return _generate_match(value[0])
    raise _UnsupportedPattern("empty character class")


class _UnsupportedPattern(Exception):
    """A pattern construct this generator does not model."""


def _path_exists(path, schemas):
    """True when every segment of a dotted placeholder path is declared.

    An indexed reference such as ``${fem_result.fixed_faces[0]}`` is accepted
    when its root array is declared: indexing a declared list is a resolution
    concern, not a declaration one.
    """
    segments = re.sub(r"\[\d+\]", "", path).split(".")
    node = schemas.get(segments[0])
    if node is None:
        return False
    for segment in segments[1:]:
        if not isinstance(node, dict):
            return False
        properties = node.get("properties") or {}
        node = properties.get(segment)
        if node is None:
            return False
    return True


def test_a_location_with_an_off_menu_kind_is_rejected():
    """`kind` is what separates a real hot spot from a restraint artefact.

    It carries the acceptance criterion, so it needs the same off-enum guard
    `verdict` has -- otherwise swapping the enum for a free-text field is a
    silent regression.
    """
    review = copy.deepcopy(GOOD)
    review["critical_locations"] = [_location(kind="looks_stressed")]
    assert _validate(review)


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
