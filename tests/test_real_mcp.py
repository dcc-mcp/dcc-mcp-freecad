"""Real SDK -> Core -> FreeCAD tests, distinct from direct-bridge GUI tests.

Selecting this marker requires a real pinned host and the test SDK. No mock or
skip can turn missing infrastructure into a native pass.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from e2e_support.artifacts import Evidence, digest
from e2e_support.cleanup import close_owned
from e2e_support.client import session_flow
from e2e_support.rejection import native_shape_rejection

from dcc_mcp_freecad import bridge as bridge_module
from dcc_mcp_freecad.bridge import FreecadBridge, WriteVerificationError
from dcc_mcp_freecad.presentation import validate_options
from dcc_mcp_freecad.server import FreecadMcpServer

pytestmark = [pytest.mark.freecad, pytest.mark.freecad_mcp]
NEEDED = [
    ("freecad-session", name)
    for name in (
        "get_status",
        "create_document",
        "inspect_document",
        "validate_document",
        "save_copy",
    )
]
NEEDED += [
    ("freecad-modeling", name)
    for name in ("add_primitive", "update_primitive", "boolean_operation")
]


def test_real_sdk_model_appearance_reopen_native_image_and_cleanup(tmp_path, monkeypatch):
    from importlib import metadata

    executable = os.environ.get("FREECAD_TEST_EXECUTABLE", "")
    version = os.environ.get("FREECAD_REAL_VERSION", "")
    assert executable and Path(executable).is_file(), "Real MCP tests require FreeCADCmd"
    assert version in ("1.0.2", "1.1.4"), "Use the exact supported CI host"
    assert os.environ.get("QT_QPA_PLATFORM") == "xcb", "Native PNG witness requires Xvfb/xcb"
    assert metadata.version("dcc-mcp-core") == "0.20.41"
    assert metadata.version("mcp") == "1.30.0"
    destination = Path(os.environ.get("FREECAD_E2E_ARTIFACT_DIR", str(tmp_path / "artifacts")))
    evidence = Evidence(destination / ("freecad-" + version))
    model, presentation = tmp_path / "model.FCStd", tmp_path / "presentation.FCStd"
    for key, value in {
        "DCC_MCP_FREECAD_EXECUTABLE": executable,
        "DCC_MCP_FREECAD_BACKEND": "freecadcmd",
        "DCC_MCP_FREECAD_ALLOWED_ROOTS": str(tmp_path),
        "DCC_MCP_FREECAD_MAX_TIMEOUT_SECS": "30",
        "DCC_MCP_DISABLE_DEFAULT_SKILL_PATHS": "1",
        "DCC_MCP_DISABLE_ACCUMULATED_SKILLS": "1",
        "DCC_MCP_DISABLE_FILE_LOGGING": "1",
        "DCC_MCP_DISABLE_JOB_PERSISTENCE": "1",
        "DCC_MCP_DISABLE_TELEMETRY": "1",
        "DCC_MCP_CHECKPOINT_IN_MEMORY": "1",
        "DCC_MCP_REGISTRY_DIR": str(tmp_path / "registry"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DCC_MCP_SKILL_PATHS", raising=False)
    children = []
    native_rejections = []
    original_invoke = FreecadBridge._invoke

    def observed_invoke(bridge, method, params, timeout_secs=120):
        try:
            return original_invoke(bridge, method, params, timeout_secs)
        except WriteVerificationError as error:
            native_rejections.append(
                {
                    "error_type": type(error).__name__,
                    "method": method,
                    "payload": deepcopy(error.payload),
                    "message": str(error),
                }
            )
            raise

    monkeypatch.setattr(FreecadBridge, "_invoke", observed_invoke)
    admission = {"open": True}
    original_process_module = bridge_module.subprocess

    def recorded_popen(command, **kwargs):
        # Return the real Popen object; do not modify stdlib or proxy poll/wait.
        assert admission["open"], "Native test admission closed"
        assert len(children) < 24, "Unexpected native expansion"
        request = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
        process = original_process_module.Popen(command, **kwargs)
        children.append((request["method"], process))
        return process

    process_module = SimpleNamespace(**vars(original_process_module))
    process_module.Popen = recorded_popen
    monkeypatch.setattr(bridge_module, "subprocess", process_module)
    report = {
        "status": "FAIL",
        "host_version": version,
        "core_version": "0.20.41",
        "mcp_sdk_version": "1.30.0",
        "capture_endpoint_claim": False,
    }
    server = handle = None
    error_type = None
    cleanup = {}

    async def scenario(client):
        status = await client.call("get_status", {})
        assert status["version"] == version and status["python_version"].startswith("3.11.")
        await client.call("create_document", {"path": str(model), "timeout_secs": 30})
        await client.call(
            "add_primitive",
            {
                "document_path": str(model),
                "primitive": "box",
                "name": "Plate",
                "dimensions": {"length": 80, "width": 50, "height": 8},
                "timeout_secs": 30,
            },
        )
        await client.call(
            "add_primitive",
            {
                "document_path": str(model),
                "primitive": "cylinder",
                "name": "Port",
                "dimensions": {"radius": 6, "height": 12},
                "translation": [40, 25, -2],
                "timeout_secs": 30,
            },
        )
        await client.call(
            "boolean_operation",
            {
                "document_path": str(model),
                "operation": "cut",
                "base_object": "Plate",
                "tool_object": "Port",
                "result_name": "Bracket",
                "timeout_secs": 30,
            },
        )
        before = await client.call("inspect_document", {"path": str(model), "timeout_secs": 30})
        await client.call(
            "update_primitive",
            {
                "document_path": str(model),
                "object_name": "Port",
                "dimensions": {"radius": 9},
                "timeout_secs": 30,
            },
        )
        after = await client.call("inspect_document", {"path": str(model), "timeout_secs": 30})

        def shape(doc):
            return next(o["shape"] for o in doc["objects"] if o["name"] == "Bracket")

        assert shape(after)["valid"] and shape(after)["closed"] and shape(after)["solids"] == 1
        assert shape(before)["volume"] - shape(after)["volume"] == pytest.approx(
            8 * math.pi * (81 - 36)
        )
        source_hash = digest(model)
        child_count = len(children)
        rejection_start = len(native_rejections)
        rejected = await client.call(
            "add_primitive",
            {
                "document_path": str(model),
                "primitive": "box",
                "name": "RejectedTinyBox",
                "dimensions": {"length": 1e-9, "width": 1e-9, "height": 1e-9},
                "timeout_secs": 30,
            },
            expect_success=False,
        )
        assert len(children) == child_count + 1, "The failure must reach real native FreeCAD"
        assert digest(model) == source_hash, "Rejected native write changed the source"
        rejection = native_shape_rejection(rejected, native_rejections[rejection_start:], version)
        report["native_rejection"] = rejection
        evidence.record("verified-rejection", "add_primitive", rejection)
        recovered = await client.call("inspect_document", {"path": str(model), "timeout_secs": 30})
        assert {o["name"] for o in recovered["objects"]} == {"Plate", "Port", "Bracket"}
        requested = [{"object_name": "Bracket", "rgb": [0.2, 0.55, 0.8], "opacity": 1.0}]
        copied = await client.call(
            "save_copy",
            {
                "source_path": str(model),
                "output_path": str(presentation),
                "overwrite": False,
                "visible_objects": ["Bracket"],
                "view": "isometric",
                "appearances": requested,
                "frame_margin": 0.12,
                "timeout_secs": 30,
            },
        )
        assert digest(model) == source_hash
        assert copied["sha256"] == digest(presentation)
        assert {"copy.geometry", "copy.presentation", "copy.presentation_request"} <= set(
            copied["verified_checks"]
        )
        final = await client.call(
            "inspect_document", {"path": str(presentation), "timeout_secs": 30}
        )
        assert shape(final)["volume"] == pytest.approx(shape(after)["volume"])
        valid = await client.call(
            "validate_document", {"path": str(presentation), "timeout_secs": 30}
        )
        assert valid["valid"] and not valid["empty_shape_objects"]
        return source_hash, copied, requested

    try:
        server = FreecadMcpServer(port=0, gateway_port=0, enable_gateway_failover=False)
        server.register_inprocess_executor(None)
        server.register_builtin_actions(include_bundled=False)
        handle = server.start(install_atexit_hook=False)
        assert handle.is_gateway is False
        source_hash, copied, requested = asyncio.run(
            asyncio.wait_for(
                session_flow(handle.mcp_url(), NEEDED, evidence.record, scenario), timeout=240
            )
        )
        evidence.stage = "independent-native-image"
        native = FreecadBridge(executable, allowed_roots=[tmp_path], max_timeout_secs=30)
        native.driver_path = Path(__file__).parent / "native/mcp_capture_host.py"
        image = native._invoke(
            "fixture.capture",
            {
                "document_path": str(presentation),
                "raw_path": str(tmp_path / "raw.png"),
                "output_path": str(tmp_path / "clean.png"),
            },
            30,
        )
        assert image["snapshot"]["host"]["version"] == version
        assert image["snapshot"]["visible_objects"] == ["Bracket"]
        expected = validate_options(["Bracket"], "isometric", requested)[0]
        assert image["snapshot"]["appearances"]["Bracket"]["rgb"] == pytest.approx(
            expected["rgb"], abs=1e-6, rel=0
        )
        assert digest(model) == source_hash and digest(presentation) == copied["sha256"]
        shutil.copyfile(tmp_path / "clean.png", evidence.directory / "native-preview.png")
        report.update(
            status="PASS",
            source_unchanged=True,
            native_rollback_verified=True,
            dependency_recompute_verified=True,
            image_rgba_sha256=image["rgba_sha256"],
            image_png_sha256=digest(evidence.directory / "native-preview.png"),
            image_size=[512, 512],
            colored_geometry=image["colored_geometry"],
            image_role=image["capture_role"],
            image_text_keys=image["text_keys"],
            effective_samples=None,
        )
    except BaseException as error:
        error_type = type(error).__name__
    finally:
        admission["open"] = False
        cleanup, terminal = close_owned(server, handle, children)
        normal = bool(terminal) and all(
            t["returncode"] == 0 and not t["cleanup_termination"] for t in terminal
        )
        report.update(
            status="PASS"
            if report["status"] == "PASS" and not error_type and normal and all(cleanup.values())
            else "FAIL",
            failed_stage=evidence.stage,
            error_type=error_type,
            cleanup=cleanup,
            native_children=terminal,
        )
        evidence.report(report)
    assert report["status"] == "PASS", (
        "Real MCP flow failed; see sanitized report.json and stage trace"
    )
