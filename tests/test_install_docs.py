from pathlib import Path


def test_install_runbook_is_wheel_first_and_platform_complete():
    root = Path(__file__).parents[1]
    text = (root / "install.md").read_text(encoding="utf-8")

    for heading in (
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ):
        assert heading in text
    for platform in ("Windows", "macOS", "Linux"):
        assert platform in text
    for command in (
        "python -m pip install --upgrade dcc-mcp-freecad",
        "dcc-mcp-freecad doctor --json",
        "dcc-mcp-freecad verify --json",
        "python -m pip uninstall dcc-mcp-freecad",
    ):
        assert command in text
    assert "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-freecad/main/install.md" in text
    assert "no adapter-managed binary cache" in text
    assert 'pip install -e ".[dev]"' not in text


def test_readme_routes_agents_to_public_doctor():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert "dcc-mcp-freecad doctor --json" in readme
    assert "dcc-mcp-freecad verify --json" in readme
    assert "install.md" in readme
