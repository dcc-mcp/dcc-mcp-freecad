"""Capability contract locks shared by the contract suite and the real-host suite.

Every lock here returns a list of human-readable problems instead of asserting,
so the same check can be pointed at the shipped catalog, at a deliberately
corrupted copy (to prove the lock actually goes red), and at the payload a real
FreeCAD host served.

The bug class being locked is the one that hit the reference project's
``freecad://capabilities``: a capability declaration that advertises something
the implementation never had. It never raises a useful error - the agent just
keeps calling the tool wrong.
"""

from __future__ import annotations

import inspect
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import jsonschema

from dcc_mcp_freecad import capabilities

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "src" / "dcc_mcp_freecad" / "skills"

_ACCEPTED_KINDS = (
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)

# scripts/*.py all funnel into one of three shapes that name the bridge method
# they dispatch to -- ``bridge_main(...)`` for the document tools,
# ``snapshot_main(...)`` for the snapshot tools (which differ only in how they
# turn a refusal into a result), and a plain ``get_bridge().<method>(...)``
# call for the parts tools, which format their own successes and refusals --
# so the method a tool actually executes is readable from its source file.
_BRIDGE_METHOD = re.compile(
    r'(?:(?:bridge|snapshot)_main\(\s*"|get_bridge\(\)\.)([A-Za-z_][A-Za-z0-9_]*)'
)


def copy_skills(destination: Path) -> Path:
    """Copy the shipped skill tree so a test can corrupt a catalog in isolation."""
    target = Path(destination) / "skills"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SKILLS, target)
    return target


def tool_catalog(skills_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    return capabilities.load_tool_catalog(skills_dir)


def capability_problems(payload: Mapping[str, Any], skills_dir: Optional[Path] = None) -> List[str]:
    """Every field where a served capability payload disagrees with tools.yaml.

    The suffix sets come from ``bridge``: they are the sets that actually
    accept or reject a path, so a capability list that advertises a format the
    bridge would refuse is drift.
    """
    from dcc_mcp_freecad.bridge import _EXPORT_SUFFIXES, _IMPORT_SUFFIXES

    return capabilities.capability_drift(
        payload,
        skills_dir=skills_dir,
        import_extensions=_IMPORT_SUFFIXES,
        export_extensions=_EXPORT_SUFFIXES,
    )


def call_example_problems(catalog: Sequence[Mapping[str, Any]]) -> List[str]:
    """Every ``call_examples`` entry must validate against its own input schema.

    Draft 7 is the dialect these schemas are written in: numeric
    ``exclusiveMinimum``, ``dependencies`` as ``dependentRequired``, and
    ``if``/``then`` are all draft-07 keywords.
    """
    problems: List[str] = []
    checked = 0
    for tool in catalog:
        schema = tool.get("input_schema") or {}
        try:
            jsonschema.Draft7Validator.check_schema(schema)
        except jsonschema.SchemaError as error:
            problems.append(
                "%s: input_schema is not valid draft-07 JSON Schema: %s"
                % (tool["name"], error.message)
            )
        for index, example in enumerate(tool.get("call_examples") or ()):
            checked += 1
            arguments = example.get("arguments") if isinstance(example, dict) else example
            label = "%s call_examples[%d]" % (tool["name"], index)
            if not isinstance(arguments, dict):
                problems.append("%s: arguments must be an object, got %r" % (label, arguments))
                continue
            validator = jsonschema.Draft7Validator(schema)
            unknown = set(arguments) - set((schema.get("properties") or {}).keys())
            for name in sorted(unknown):
                problems.append(
                    "%s: passes %r, which input_schema does not declare" % (label, name)
                )
            for error in sorted(validator.iter_errors(arguments), key=str):
                problems.append("%s: %s" % (label, error.message))
    if not checked:
        problems.append("no call examples were collected; the example lock ran vacuously")
    return problems


def read_only_path_problems(catalog: Sequence[Mapping[str, Any]]) -> List[str]:
    """A ``read_only_hint: true`` tool must not sit on a path that writes.

    Two locks in one pass. A ``next-tools`` target that tools.yaml does not
    declare is the declaration bug in its purest form - a name the catalog has
    never heard of - so it is reported for every tool, read-only or not. And a
    read-only tool whose *remediation* chain routes into a writing tool is a
    read-only claim that does not survive the branch where it failed.

    Only ``on-failure`` is checked. A read-only discovery tool naming the
    writer that consumes its result (``list_parts`` -> ``insert_part``) is the
    intended workflow rather than a broken claim: the read-only tool still does
    no writing, and the caller is being handed the next step. What must never
    happen is a tool that reports a problem sending the caller into a write to
    recover from it, because there the read-only hint is what told the caller
    no write was coming.
    """
    by_name = {str(tool["name"]): tool for tool in catalog}
    problems: List[str] = []
    for tool in catalog:
        name = str(tool["name"])
        annotations = tool.get("annotations") or {}
        read_only = bool(annotations.get("read_only_hint"))
        for phase, targets in (tool.get("next-tools") or {}).items():
            for target in targets:
                if target not in by_name:
                    problems.append(
                        "%s: next-tools %s routes to %r, which tools.yaml does not declare"
                        % (name, phase, target)
                    )
                    continue
                routed = by_name[target].get("annotations") or {}
                if read_only and phase != "on-success" and not routed.get("read_only_hint"):
                    problems.append(
                        "%s: read_only_hint=true but next-tools %s routes into writing tool %s"
                        % (name, phase, target)
                    )
        if not read_only:
            continue
        if annotations.get("destructive_hint") is not False:
            problems.append("%s: read_only_hint=true but destructive_hint is not false" % name)
        if annotations.get("idempotent_hint") is not True:
            problems.append("%s: read_only_hint=true but idempotent_hint is not true" % name)
    return problems


def declaration_problems(
    catalog: Sequence[Mapping[str, Any]], skills_dir: Optional[Path] = None
) -> List[str]:
    """Every argument a tool declares must be one its implementation accepts.

    This is the lock for the reference bug: a declaration is only trustworthy
    if the parameter it advertises exists on the callable that runs. The
    reverse direction is deliberately not checked - ``additionalProperties:
    false`` already stops a caller passing an undeclared argument, so a
    parameter the schema omits is unreachable rather than a false promise.
    """
    from dcc_mcp_freecad.bridge import FreecadBridge

    root = Path(skills_dir) if skills_dir is not None else SKILLS
    problems: List[str] = []
    for tool in catalog:
        name = str(tool["name"])
        source = root / str(tool.get("skill")) / str(tool.get("source_file"))
        if not source.is_file():
            problems.append("%s: source_file %s does not exist" % (name, tool.get("source_file")))
            continue
        match = _BRIDGE_METHOD.search(source.read_text(encoding="utf-8"))
        if match is None:
            problems.append(
                "%s: %s declares no bridge or snapshot entry method" % (name, source.name)
            )
            continue
        method_name = match.group(1)
        method = getattr(FreecadBridge, method_name, None)
        if method is None or not callable(method):
            problems.append("%s: FreecadBridge has no method %r" % (name, method_name))
            continue
        parameters = inspect.signature(method).parameters
        accepted = {
            parameter_name
            for parameter_name, parameter in parameters.items()
            if parameter_name != "self" and parameter.kind in _ACCEPTED_KINDS
        }
        schema = tool.get("input_schema") or {}
        declared = set((schema.get("properties") or {}).keys())
        for argument in sorted(declared - accepted):
            problems.append(
                "%s: input_schema declares %r but FreecadBridge.%s does not accept it"
                % (name, argument, method_name)
            )
        for argument in sorted(set(schema.get("required") or ()) - accepted):
            problems.append(
                "%s: input_schema requires %r but FreecadBridge.%s does not accept it"
                % (name, argument, method_name)
            )
    return problems


def all_problems(payload: Mapping[str, Any], skills_dir: Optional[Path] = None) -> List[str]:
    """Run every lock against one served payload and its catalog."""
    catalog = tool_catalog(skills_dir)
    return (
        capability_problems(payload, skills_dir)
        + read_only_path_problems(catalog)
        + call_example_problems(catalog)
        + declaration_problems(catalog, skills_dir)
    )
