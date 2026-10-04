"""Thin compatibility facade for the shared Install SOP v1 contract."""

from __future__ import annotations

import dcc_mcp_core as _core

# Value of the report document's own `schema_version` field. Kept in sync with
# `load_install_sop_schema()["properties"]["schema_version"]["const"]` by
# tests/test_doctor.py, which fails when the resolved core drifts.
#
# This is deliberately NOT the revision of the published schema *artifact* (the
# `-vN` suffix of `adapter-install-sop-vN.schema.json`). That is a separate
# counter -- 2 since dcc-mcp-core 0.20.36, named `INSTALL_SOP_SCHEMA_REVISION`
# from 0.20.40 -- and it moves independently, because an artifact revision only
# ever adds optional members and so never changes what a document means.
# Emitting it as `schema_version` makes every doctor/verify report fail
# validation the moment the resolved core advances. Nothing in this adapter
# reads the artifact revision, so it is not mirrored here at all: a re-export
# would only be a second binding to rename the next time Core moves it.
SCHEMA_VERSION = 1

EXIT_OK = _core.INSTALL_EXIT_OK
EXIT_PREFLIGHT = _core.INSTALL_EXIT_PREFLIGHT
EXIT_VERIFY = _core.INSTALL_EXIT_VERIFY


def runtime_core_version() -> str:
    return str(getattr(_core, "__version__", "unavailable"))
