"""Thin compatibility facade for the shared Install SOP v1 contract."""

from __future__ import annotations

import dcc_mcp_core as _core

# `INSTALL_SOP_SCHEMA_VERSION` is the revision of the published Install SOP
# schema *artifact* (`adapter-install-sop-vN.schema.json`), 2 since
# dcc-mcp-core 0.20.36. It is NOT the value of the `schema_version` field that
# the artifact pins on a report document: that field is a separate, stable
# counter declared as `properties.schema_version.const` and stays at 1, because
# artifact revisions only add optional members. The two are named separately
# here -- conflating them makes every doctor/verify report fail validation the
# moment the resolved core advances.
ARTIFACT_SCHEMA_VERSION = _core.INSTALL_SOP_SCHEMA_VERSION

# Value of the report document's own `schema_version` field. Kept in sync with
# `load_install_sop_schema()["properties"]["schema_version"]["const"]` by
# tests/test_doctor.py, which fails when the resolved core drifts.
SCHEMA_VERSION = 1

EXIT_OK = _core.INSTALL_EXIT_OK
EXIT_PREFLIGHT = _core.INSTALL_EXIT_PREFLIGHT
EXIT_VERIFY = _core.INSTALL_EXIT_VERIFY


def runtime_core_version() -> str:
    return str(getattr(_core, "__version__", "unavailable"))
