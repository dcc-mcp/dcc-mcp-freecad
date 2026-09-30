from pathlib import Path

import yaml
from dcc_mcp_core import validate_skill

from dcc_mcp_freecad.server import FreecadMcpServer

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "src" / "dcc_mcp_freecad" / "skills"


def test_skill_contracts_are_valid():
    for name in ("freecad-session", "freecad-modeling"):
        report = validate_skill(str(SKILLS / name))
        errors = [issue.message for issue in report.issues if issue.severity == "error"]
        assert errors == [], name


def test_all_tools_are_typed_bounded_and_affinity_explicit():
    tools = []
    for name in ("freecad-session", "freecad-modeling"):
        payload = yaml.safe_load((SKILLS / name / "tools.yaml").read_text(encoding="utf-8"))
        tools.extend(payload["tools"])

    assert len(tools) == 13
    assert len({tool["name"] for tool in tools}) == 13
    for tool in tools:
        assert tool["input_schema"]["type"] == "object"
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["output_schema"]["type"] == "object"
        assert tool["affinity"] == "any"
        assert tool["enforce_thread_affinity"] is True
        assert "timeout_hint_secs" in tool
        assert set(tool["annotations"]) == {
            "read_only_hint",
            "destructive_hint",
            "idempotent_hint",
            "open_world_hint",
            "deferred_hint",
        }


def test_modeling_declares_document_dependency():
    frontmatter = (SKILLS / "freecad-modeling" / "SKILL.md").read_text(encoding="utf-8")
    assert "depends: [freecad-session]" in frontmatter


def test_driver_exposes_only_a_method_whitelist():
    driver = (ROOT / "src" / "dcc_mcp_freecad" / "freecad_driver.py").read_text(encoding="utf-8")
    assert "_METHODS = {" in driver
    assert "eval(" not in driver
    assert "exec(" not in driver


def test_server_declares_standalone_lifetime():
    server = FreecadMcpServer(port=0)
    options = next(value for value in vars(server).values() if hasattr(value, "instance_type"))
    assert options.instance_type == "standalone"


def test_report_schema_version_is_the_const_and_not_the_artifact_revision():
    """The two schema counters must never collapse back into one name.

    `dcc-mcp-core` publishes an Install SOP *artifact* whose revision moved to 2
    in 0.20.36, while the documents it describes keep `schema_version: 1`. Every
    report emitted between 0.3.0 and 0.4.1 carried the artifact revision, so the
    reports failed the very schema they claimed to follow. Naming them apart is
    what stops that from recurring; these assertions keep them apart.
    """
    import dcc_mcp_core

    from dcc_mcp_freecad import install_contract

    assert install_contract.ARTIFACT_SCHEMA_VERSION == dcc_mcp_core.INSTALL_SOP_SCHEMA_VERSION
    schema = install_contract.load_install_sop_schema()
    assert schema["properties"]["schema_version"]["const"] == install_contract.SCHEMA_VERSION


def test_doctor_never_stamps_the_artifact_schema_revision():
    doctor = (ROOT / "src" / "dcc_mcp_freecad" / "doctor.py").read_text(encoding="utf-8")
    assert "ARTIFACT_SCHEMA_VERSION" not in doctor
