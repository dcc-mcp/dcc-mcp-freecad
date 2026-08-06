# dcc-mcp-freecad

FreeCAD adapter foundation for the DCC-MCP organization.

This is an experimental, read-only first slice. It is **not** in the released
`dcc-mcp-cli dcc-types` catalog yet.

## Scope

- Discover the console/standalone boundary.
- Expose one typed, read-only document inspection tool.
- Keep host API calls outside the MCP HTTP worker.
- Do not expose arbitrary source evaluation.

## Install

```bash
python -m pip install -e ".[test]"
dcc-mcp-freecad
```

Configure the bridge environment variables in `src/dcc_mcp_freecad/bridge.py`.
A real FreeCAD live smoke is required before catalog onboarding.

Official API reference: https://www.freecad.org/api/

