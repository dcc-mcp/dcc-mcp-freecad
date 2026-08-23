"""Thin compatibility facade for the shared Install SOP v1 contract."""

from __future__ import annotations

try:
    import dcc_mcp_core as _core

    SCHEMA_VERSION = _core.INSTALL_SOP_SCHEMA_VERSION
    EXIT_OK = _core.INSTALL_EXIT_OK
    EXIT_PREFLIGHT = _core.INSTALL_EXIT_PREFLIGHT
    EXIT_VERIFY = _core.INSTALL_EXIT_VERIFY
except AttributeError:  # Compatibility until Core #2252 is in the minimum release.
    SCHEMA_VERSION = 1
    EXIT_OK, EXIT_PREFLIGHT, EXIT_VERIFY = 0, 10, 40


def runtime_core_version() -> str:
    return str(getattr(_core, "__version__", "unavailable"))
