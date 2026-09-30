"""Thin compatibility facade for the shared Install SOP v1 contract."""

from __future__ import annotations

from typing import Any

import dcc_mcp_core as _core

# Two different counters share the name "schema version" in this contract, and
# conflating them silently invalidates every report the adapter emits.
#
#   ARTIFACT_SCHEMA_VERSION -- the revision of the schema *artifact* that
#     dcc-mcp-core publishes. It is 2 since core 0.20.36 and moves whenever the
#     artifact's own shape changes.
#   SCHEMA_VERSION -- the value that artifact pins on the `schema_version` field
#     of a report document (`properties.schema_version.const`, currently 1). It
#     is a separate, stable counter: a v2 artifact still describes documents
#     whose schema_version is 1.
#
# They are named apart so the report field can never again be sourced from the
# artifact revision. `doctor.py` re-checks SCHEMA_VERSION against the artifact
# published by the resolved core before it emits anything, so a future core that
# moves the const fails the preflight instead of shipping invalid reports.
ARTIFACT_SCHEMA_VERSION = _core.INSTALL_SOP_SCHEMA_VERSION
SCHEMA_VERSION = 1

EXIT_OK = _core.INSTALL_EXIT_OK
EXIT_PREFLIGHT = _core.INSTALL_EXIT_PREFLIGHT
EXIT_VERIFY = _core.INSTALL_EXIT_VERIFY


def runtime_core_version() -> str:
    return str(getattr(_core, "__version__", "unavailable"))


def load_install_sop_schema() -> dict[str, Any]:
    """Return the Install SOP schema artifact published by the resolved core."""
    return _core.load_install_sop_schema()
