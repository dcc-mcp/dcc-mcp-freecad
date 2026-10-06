"""Standalone FreeCAD doctor and verify orchestration.

Host support is decided by the machine-readable matrix in ``compat_matrix.json``
(see ``compat.py``), never by a hardcoded minimum: an undeclared FreeCAD version
is rejected with an explicit error code instead of being assumed compatible.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from .__version__ import __version__
from .bridge import BridgeError, FreecadBridge
from .compat import (
    SUPPORTED,
    TOO_NEW,
    UNKNOWN,
    UNLISTED,
    classify_host,
    load_matrix,
    supported_range_labels,
    unsupported_reason,
)
from .install_contract import (
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_VERIFY,
    SCHEMA_VERSION,
    runtime_core_version,
)

MIN_CORE_VERSION = "0.20.36"
_RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

# Machine-readable error codes for host support verdicts.
ERROR_HOST_TOO_OLD = "freecad_host_version_unsupported"
ERROR_HOST_TOO_NEW = "freecad_host_version_unverified"
ERROR_HOST_UNLISTED = "freecad_host_version_unlisted"
ERROR_HOST_UNKNOWN = "freecad_host_version_unparsable"

_HOST_ERROR_CODES = {
    TOO_NEW: ERROR_HOST_TOO_NEW,
    UNLISTED: ERROR_HOST_UNLISTED,
    UNKNOWN: ERROR_HOST_UNKNOWN,
}


def _host_error_code(status):
    return _HOST_ERROR_CODES.get(status, ERROR_HOST_TOO_OLD)


def _release_tuple(value: str) -> Optional[tuple[int, int, int]]:
    match = _RELEASE.match(value.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def _install_freecad_step() -> dict[str, Any]:
    ranges = ", ".join(supported_range_labels(load_matrix())) or "a supported version"
    if os.name == "nt" and shutil.which("winget"):
        command = ["winget", "install", "--id", "FreeCAD.FreeCAD", "--exact"]
    elif sys.platform == "darwin" and shutil.which("brew"):
        command = ["brew", "install", "--cask", "freecad"]
    elif shutil.which("apt-get"):
        command = ["sudo", "apt-get", "install", "freecad"]
    elif shutil.which("dnf"):
        command = ["sudo", "dnf", "install", "freecad"]
    else:
        command = [sys.executable, "-m", "webbrowser", "https://www.freecad.org/downloads.php"]
    return {
        "id": "install-freecad",
        "description": "Install or pin an OS-managed FreeCAD in the supported range: %s" % ranges,
        "command": command,
        "why": "The standalone adapter requires a local FreeCADCmd executable",
    }


def _command_step(
    identifier: str, description: str, command: list[str], why: str
) -> dict[str, Any]:
    return {"id": identifier, "description": description, "command": command, "why": why}


def _report(
    verb: str,
    core_version: str,
    checks: dict[str, Any],
    steps: list[dict[str, Any]],
    exit_code: int,
    failure_stage: Optional[str] = None,
    failure_reason: Optional[str] = None,
    next_steps: Optional[list[dict[str, Any]]] = None,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    directly_usable = exit_code == EXIT_OK
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if directly_usable else "failed",
        "error_code": error_code,
        "dcc_type": "freecad",
        "verb": verb,
        "adapter_version": __version__,
        "core_version": core_version,
        "checks": checks,
        "steps": steps,
        "next_steps": list(next_steps or ()),
        "receipt_path": None,
        "verify": {
            "directly_usable": directly_usable,
            "failure_stage": failure_stage,
            "failure_reason": failure_reason,
        },
        "_exit_code": exit_code,
    }


def doctor_report(
    executable: Optional[Path] = None, verb: str = "doctor", timeout: float = 30
) -> dict[str, Any]:
    core_version = runtime_core_version()
    checks: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    try:
        bridge = FreecadBridge.from_env(str(executable) if executable is not None else None)
    except (OSError, ValueError) as exc:
        reason = "Invalid FreeCAD adapter configuration: %s" % exc
        checks["configuration"] = {"success": False, "reason": str(exc)}
        steps.append({"id": "validate-configuration", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "configuration",
            reason,
            [
                _command_step(
                    "review-configuration",
                    "Correct the FreeCAD environment configuration",
                    ["dcc-mcp-freecad", verb, "--json"],
                    reason,
                )
            ],
        )

    roots_valid = all(root.is_dir() for root in bridge.allowed_roots)
    timeout_valid = 0 < timeout <= bridge.max_timeout_secs
    port_value = os.environ.get("DCC_MCP_FREECAD_PORT", "").strip()
    try:
        server_port = int(port_value) if port_value else None
        port_valid = server_port is None or 0 <= server_port <= 65_535
    except ValueError:
        server_port = port_value
        port_valid = False
    checks["configuration"] = {
        "success": roots_valid and timeout_valid and port_valid,
        "allowed_roots": [str(root) for root in bridge.allowed_roots],
        "max_document_bytes": bridge.max_document_bytes,
        "max_timeout_secs": bridge.max_timeout_secs,
        "probe_timeout_secs": timeout,
        "server_port": server_port,
    }
    if not checks["configuration"]["success"]:
        if not roots_valid:
            reason = "Every DCC_MCP_FREECAD_ALLOWED_ROOTS entry must be an existing directory"
        elif not timeout_valid:
            reason = "Probe timeout must be greater than 0 and no more than %s seconds" % int(
                bridge.max_timeout_secs
            )
        else:
            reason = "DCC_MCP_FREECAD_PORT must be an integer from 0 through 65535"
        steps.append({"id": "validate-configuration", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "configuration",
            reason,
            [
                _command_step(
                    "review-configuration",
                    "Correct the FreeCAD environment configuration",
                    ["dcc-mcp-freecad", verb, "--json"],
                    reason,
                )
            ],
        )
    steps.append({"id": "validate-configuration", "status": "ok"})

    core_release = _release_tuple(core_version)
    minimum_core = _release_tuple(MIN_CORE_VERSION)
    core_compatible = (
        core_release is not None and minimum_core is not None and core_release >= minimum_core
    )
    checks["core"] = {
        "success": core_compatible,
        "version": core_version,
        "minimum": MIN_CORE_VERSION,
    }
    if not core_compatible:
        reason = "dcc-mcp-core %s is unsupported; %s or newer is required" % (
            core_version,
            MIN_CORE_VERSION,
        )
        steps.append({"id": "validate-core", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "core",
            reason,
            [
                _command_step(
                    "upgrade-core",
                    "Upgrade dcc-mcp-core",
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "dcc-mcp-core>=%s" % MIN_CORE_VERSION,
                    ],
                    reason,
                )
            ],
        )
    steps.append({"id": "validate-core", "status": "ok"})

    checks["executable"] = {"success": bool(bridge.executable), "path": bridge.executable}
    if not bridge.executable:
        reason = "FreeCADCmd was not found"
        steps.append({"id": "discover-freecad", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "host",
            reason,
            [_install_freecad_step()],
        )
    steps.append({"id": "discover-freecad", "status": "ok"})

    checks["driver"] = {"success": bridge.driver_path.is_file(), "path": str(bridge.driver_path)}
    if not checks["driver"]["success"]:
        reason = "The packaged FreeCAD driver is missing"
        steps.append({"id": "validate-driver", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "driver",
            reason,
            [
                _command_step(
                    "reinstall-adapter",
                    "Reinstall the adapter wheel",
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--force-reinstall",
                        "dcc-mcp-freecad",
                    ],
                    reason,
                )
            ],
        )
    steps.append({"id": "validate-driver", "status": "ok"})

    try:
        runtime = bridge.status(timeout)
    except (BridgeError, OSError, ValueError) as exc:
        reason = "FreeCAD runtime verification failed: %s" % exc
        checks["runtime"] = {"success": False, "reason": str(exc)}
        steps.append({"id": "verify-runtime", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_VERIFY,
            "runtime",
            reason,
            [
                _command_step(
                    "retry-verify",
                    "Retry standalone runtime verification",
                    ["dcc-mcp-freecad", verb, "--json", "--dcc-path", bridge.executable],
                    reason,
                )
            ],
        )

    freecad_version = str(runtime.get("version", ""))
    python_version = str(runtime.get("python_version", ""))
    if runtime.get("ready") is not True or not python_version:
        reason = "FreeCADCmd did not report a ready runtime and embedded Python version"
        checks["runtime"] = {
            "success": False,
            "freecad_version": freecad_version,
            "python_version": python_version,
            "ready": runtime.get("ready"),
        }
        steps.append({"id": "verify-runtime", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_VERIFY,
            "runtime",
            reason,
            [
                _command_step(
                    "retry-verify",
                    "Retry standalone runtime verification",
                    ["dcc-mcp-freecad", verb, "--json", "--dcc-path", bridge.executable],
                    reason,
                )
            ],
        )
    if _release_tuple(freecad_version) is None:
        reason = "FreeCADCmd did not report a valid runtime version"
        checks["runtime"] = {"success": False, "freecad_version": freecad_version}
        steps.append({"id": "verify-runtime", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_VERIFY,
            "runtime",
            reason,
            [_install_freecad_step()],
            ERROR_HOST_UNKNOWN,
        )
    verdict = classify_host(freecad_version)
    verdict["api_probe"] = runtime.get("api_probe") or []
    checks["runtime"] = {
        "success": verdict["status"] == SUPPORTED,
        "freecad_version": freecad_version,
        "python_version": python_version,
        "host_matrix": verdict,
    }
    if not checks["runtime"]["success"]:
        reason = unsupported_reason(verdict)
        steps.append({"id": "verify-runtime", "status": "failed", "message": reason})
        return _report(
            verb,
            core_version,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "host_version",
            reason,
            [
                _install_freecad_step(),
                _command_step(
                    "recheck-host-matrix",
                    "Re-run the compatibility preflight against the pinned executable",
                    ["dcc-mcp-freecad", verb, "--json", "--dcc-path", bridge.executable],
                    reason,
                ),
            ],
            _host_error_code(verdict["status"]),
        )
    steps.append({"id": "verify-runtime", "status": "ok"})
    steps.append(
        {
            "id": "verify-host-matrix",
            "status": "ok",
            "message": "FreeCAD %s is inside the verified compatibility matrix (%s)"
            % (freecad_version, ", ".join(verdict["supported_ranges"])),
        }
    )
    return _report(verb, core_version, checks, steps, EXIT_OK)
