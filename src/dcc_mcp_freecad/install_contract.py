"""Thin compatibility facade for the shared Install SOP v1 contract."""

from __future__ import annotations

import dcc_mcp_core as _core

SCHEMA_VERSION = _core.INSTALL_SOP_SCHEMA_VERSION
EXIT_OK = _core.INSTALL_EXIT_OK
EXIT_PREFLIGHT = _core.INSTALL_EXIT_PREFLIGHT
EXIT_VERIFY = _core.INSTALL_EXIT_VERIFY


def runtime_core_version() -> str:
    return str(getattr(_core, "__version__", "unavailable"))
