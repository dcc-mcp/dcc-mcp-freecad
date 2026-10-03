from types import SimpleNamespace

import pytest

from dcc_mcp_freecad.skill_tools import bridge_main


@pytest.mark.parametrize("checks", [[], ["dimensions", "placement", "shape_validity"]])
def test_native_check_list_does_not_collide_with_core_boolean(checks, monkeypatch):
    payload = {"verified": checks, "object": {"name": "Part"}}
    monkeypatch.setattr(
        "dcc_mcp_freecad.skill_tools.get_bridge",
        lambda: SimpleNamespace(add_primitive=lambda **_: payload),
    )
    result = bridge_main("add_primitive", "Part created")()
    assert result["success"] is True
    assert result["context"]["verified_checks"] == checks
    assert result["postcondition"]["verified"] is bool(checks)
    assert payload["verified"] == checks


def test_read_result_does_not_invent_write_verification(monkeypatch):
    monkeypatch.setattr(
        "dcc_mcp_freecad.skill_tools.get_bridge",
        lambda: SimpleNamespace(status=lambda **_: {"ready": True}),
    )
    result = bridge_main("status", "Status")()
    assert result["context"]["ready"] is True
    assert "postcondition" not in result
