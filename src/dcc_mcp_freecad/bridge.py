from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

_DOCUMENT_SUFFIX = ".fcstd"
_IMPORT_SUFFIXES = {".brep", ".brp", ".iges", ".igs", ".obj", ".step", ".stl", ".stp"}
_EXPORT_SUFFIXES = set(_IMPORT_SUFFIXES)
_OBJECT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class BridgeError(RuntimeError):
    """A bounded FreeCAD bridge failure safe to return to a local caller."""


class BridgeTimeoutError(BridgeError):
    """FreeCADCmd exceeded the configured deadline."""


def _within(path: Path, roots: Sequence[Path]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        root_value = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate, root_value)) == root_value:
                return True
        except ValueError:
            continue
    return False


def _split_roots(value: str) -> list[Path]:
    return [
        Path(item.strip()).expanduser().resolve()
        for item in value.split(os.pathsep)
        if item.strip()
    ]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_staged_path(value: Any, staged: Path, final: Path) -> Any:
    if isinstance(value, dict):
        return {key: _replace_staged_path(item, staged, final) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_staged_path(item, staged, final) for item in value]
    if isinstance(value, str):
        return value.replace(str(staged), str(final)).replace(
            str(staged).replace("\\", "/"), str(final).replace("\\", "/")
        )
    return value


def _remove_staged_backups(staged: Path) -> None:
    """Remove only FreeCAD backups derived from a unique staged filename."""
    prefix = "%s." % staged.stem.lower()
    for candidate in staged.parent.iterdir():
        name = candidate.name.lower()
        if candidate.is_file() and name.startswith(prefix) and name.endswith(".fcbak"):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


class FreecadBridge:
    """Run typed, package-owned operations in an isolated FreeCADCmd process."""

    def __init__(
        self,
        executable: Optional[str] = None,
        allowed_roots: Optional[Iterable[Path]] = None,
        max_document_bytes: int = 2 * 1024 * 1024 * 1024,
        max_timeout_secs: float = 1_800,
    ):
        self.executable = self._resolve_executable(executable)
        roots = list(allowed_roots or (Path.cwd(),))
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.max_document_bytes = max(1, int(max_document_bytes))
        self.max_timeout_secs = max(1.0, float(max_timeout_secs))
        self.driver_path = Path(__file__).with_name("freecad_driver.py").resolve()

    @classmethod
    def from_env(cls, executable: Optional[str] = None) -> "FreecadBridge":
        roots_value = os.environ.get("DCC_MCP_FREECAD_ALLOWED_ROOTS", "")
        roots = _split_roots(roots_value) if roots_value else [Path.cwd().resolve()]
        max_document_bytes = int(
            os.environ.get("DCC_MCP_FREECAD_MAX_DOCUMENT_BYTES", str(2 * 1024**3))
        )
        max_timeout_secs = float(os.environ.get("DCC_MCP_FREECAD_MAX_TIMEOUT_SECS", "1800"))
        if max_document_bytes <= 0 or max_timeout_secs <= 0:
            raise ValueError("FreeCAD document and timeout limits must be positive")
        return cls(
            executable or os.environ.get("DCC_MCP_FREECAD_EXECUTABLE") or None,
            allowed_roots=roots,
            max_document_bytes=max_document_bytes,
            max_timeout_secs=max_timeout_secs,
        )

    @staticmethod
    def _resolve_executable(explicit: Optional[str]) -> Optional[str]:
        candidates = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        else:
            for name in ("FreeCADCmd", "freecadcmd"):
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
            if os.name == "nt":
                program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                candidates.extend(
                    (
                        program_files / "FreeCAD 1.1" / "bin" / "FreeCADCmd.exe",
                        program_files / "FreeCAD 1.0" / "bin" / "FreeCADCmd.exe",
                        program_files / "FreeCAD" / "bin" / "FreeCADCmd.exe",
                    )
                )
            elif sys.platform == "darwin":
                candidates.extend(
                    (
                        Path("/Applications/FreeCAD.app"),
                        Path.home() / "Applications" / "FreeCAD.app",
                    )
                )
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_dir():
                options = (
                    candidate / "FreeCADCmd.exe",
                    candidate / "bin" / "FreeCADCmd.exe",
                    candidate / "freecadcmd",
                    candidate / "bin" / "freecadcmd",
                    candidate / "Contents" / "Resources" / "bin" / "FreeCADCmd",
                    candidate / "Contents" / "Resources" / "bin" / "freecadcmd",
                    candidate / "Contents" / "MacOS" / "FreeCADCmd",
                )
                candidate = next((item for item in options if item.is_file()), candidate)
            if candidate.is_file():
                return str(candidate)
        return None

    def _timeout(self, value: float) -> float:
        timeout = float(value)
        if timeout <= 0 or timeout > self.max_timeout_secs:
            raise BridgeError(
                "timeout_secs must be greater than 0 and no more than %s"
                % int(self.max_timeout_secs)
            )
        return timeout

    def _document_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != _DOCUMENT_SUFFIX:
            raise BridgeError("FreeCAD document paths must end with .FCStd")
        if not path.is_file():
            raise BridgeError("FreeCAD document does not exist: %s" % path)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Document is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        if path.stat().st_size > self.max_document_bytes:
            raise BridgeError("FreeCAD document exceeds the configured size limit")
        return path

    def _output_path(self, value: str, suffixes: set[str]) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in suffixes:
            raise BridgeError("Unsupported output extension: %s" % path.suffix)
        if not path.parent.is_dir():
            raise BridgeError("Output directory does not exist: %s" % path.parent)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Output is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        return path

    def _input_path(self, value: str, suffixes: set[str]) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in suffixes:
            raise BridgeError("Unsupported input extension: %s" % path.suffix)
        if not path.is_file():
            raise BridgeError("Input geometry does not exist: %s" % path)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Input geometry is outside DCC_MCP_FREECAD_ALLOWED_ROOTS")
        return path

    @staticmethod
    def _object_name(value: str) -> str:
        if not _OBJECT_NAME.fullmatch(value):
            raise BridgeError(
                "Object names must start with a letter or underscore and contain at most "
                "64 letters, digits, or underscores"
            )
        return value

    def _invoke(
        self, method: str, params: Mapping[str, Any], timeout_secs: float = 120
    ) -> dict[str, Any]:
        if not self.executable:
            raise BridgeError("FreeCADCmd was not found; set DCC_MCP_FREECAD_EXECUTABLE")
        if not self.driver_path.is_file():
            raise BridgeError("Packaged FreeCAD driver is missing")
        timeout = self._timeout(timeout_secs)
        with tempfile.TemporaryDirectory(prefix="dcc-mcp-freecad-") as temp_dir_value:
            temp_dir = Path(temp_dir_value)
            request_path = temp_dir / "request.json"
            result_path = temp_dir / "result.json"
            config_path = temp_dir / "user.cfg"
            request_path.write_text(
                json.dumps({"method": method, "params": dict(params)}, ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                self.executable,
                "--safe-mode",
                "--user-cfg",
                str(config_path),
                str(self.driver_path),
                "--pass",
                str(request_path),
                str(result_path),
            ]
            started = time.monotonic()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            environment = os.environ.copy()
            environment.setdefault("QT_QPA_PLATFORM", "offscreen")
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file:
                with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
                    process = subprocess.Popen(
                        command,
                        cwd=str(temp_dir),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        text=True,
                        creationflags=creationflags,
                    )
                    try:
                        deadline = started + timeout
                        while process.poll() is None:
                            check_dcc_cancelled()
                            if time.monotonic() >= deadline:
                                raise BridgeTimeoutError(
                                    "FreeCADCmd exceeded the %.1f second timeout" % timeout
                                )
                            time.sleep(0.05)
                    except BaseException:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read(65_537)
                    stderr = stderr_file.read(65_537)
            if not result_path.is_file():
                raise BridgeError(
                    "FreeCADCmd did not return a result (exit %s): %s"
                    % (process.returncode, (stderr or stdout).strip()[:1_000])
                )
            if result_path.stat().st_size > 16 * 1024 * 1024:
                raise BridgeError("FreeCAD result exceeded the 16 MiB response limit")
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if not payload.get("ok"):
                error = payload.get("error") or {}
                raise BridgeError(str(error.get("message") or "FreeCAD operation failed"))
            result = payload.get("result")
            if not isinstance(result, dict):
                result = {"result": result}
            result.update(
                {
                    "duration_secs": round(time.monotonic() - started, 3),
                    "stdout": stdout[:65_536],
                    "stderr": stderr[:65_536],
                    "stdout_truncated": len(stdout) > 65_536,
                    "stderr_truncated": len(stderr) > 65_536,
                }
            )
            return result

    def _mutate_document(
        self,
        method: str,
        document_path: str,
        params: Mapping[str, Any],
        timeout_secs: float,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % document.stem,
            suffix=document.suffix,
            dir=str(document.parent),
        )
        os.close(descriptor)
        staged = Path(temp_name)
        try:
            shutil.copy2(str(document), str(staged))
            request = dict(params)
            request["document_path"] = str(staged)
            result = self._invoke(method, request, timeout_secs)
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD mutation did not produce a durable document")
            os.replace(str(staged), str(document))
            result = _replace_staged_path(result, staged, document)
            durable = self._invoke(
                "document.inspect", {"document_path": str(document)}, timeout_secs
            )
            transport_keys = {
                "duration_secs",
                "stderr",
                "stderr_truncated",
                "stdout",
                "stdout_truncated",
            }
            result["document"] = {
                key: value for key, value in durable.items() if key not in transport_keys
            }
            result.update(
                {
                    "document_path": str(document),
                    "document_bytes": document.stat().st_size,
                    "document_sha256": _sha256_file(document),
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()
            _remove_staged_backups(staged)

    def status(self, timeout_secs: float = 30) -> dict[str, Any]:
        if not self.executable:
            return {
                "ready": False,
                "executable": None,
                "instance_type": "standalone",
                "reason": "freecadcmd_not_found",
                "allowed_roots": [str(root) for root in self.allowed_roots],
            }
        result = self._invoke("system.status", {}, timeout_secs)
        result.update(
            {
                "ready": True,
                "executable": self.executable,
                "instance_type": "standalone",
                "allowed_roots": [str(root) for root in self.allowed_roots],
                "max_document_bytes": self.max_document_bytes,
                "max_timeout_secs": self.max_timeout_secs,
            }
        )
        return result

    def capabilities(self) -> dict[str, Any]:
        return {
            "status": self.status(),
            "methods": [
                "create_document",
                "inspect_document",
                "validate_document",
                "save_copy",
                "add_primitive",
                "update_primitive",
                "transform_object",
                "boolean_operation",
                "remove_object",
                "import_geometry",
                "export_geometry",
            ],
            "primitives": ["box", "cone", "cylinder", "sphere", "torus"],
            "boolean_operations": ["cut", "intersection", "union"],
            "import_extensions": sorted(_IMPORT_SUFFIXES),
            "export_extensions": sorted(_EXPORT_SUFFIXES),
            "atomic_document_mutations": True,
            "arbitrary_python": False,
        }

    def create_document(
        self,
        path: str,
        overwrite: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        output = self._output_path(path, {_DOCUMENT_SUFFIX})
        name = re.sub(r"[^A-Za-z0-9_]", "_", output.stem)
        if not name or name[0].isdigit():
            name = "Document_%s" % name
        replaced_existing = output.exists()
        if replaced_existing and not overwrite:
            raise BridgeError("Document already exists; set overwrite=true to replace it")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output.stem, suffix=output.suffix, dir=str(output.parent)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        try:
            result = self._invoke(
                "document.create",
                {"document_path": str(staged), "name": name},
                timeout_secs,
            )
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD did not create a durable document")
            os.replace(str(staged), str(output))
            durable = self._invoke("document.inspect", {"document_path": str(output)}, timeout_secs)
            if "duration_secs" in result:
                durable["create_duration_secs"] = result["duration_secs"]
            result = durable
            result.update(
                {
                    "document_path": str(output),
                    "document_bytes": output.stat().st_size,
                    "document_sha256": _sha256_file(output),
                    "overwritten": replaced_existing,
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()
            _remove_staged_backups(staged)

    def inspect_document(self, path: str, timeout_secs: float = 120) -> dict[str, Any]:
        document = self._document_path(path)
        result = self._invoke("document.inspect", {"document_path": str(document)}, timeout_secs)
        result.update(
            {
                "document_path": str(document),
                "document_bytes": document.stat().st_size,
                "document_sha256": _sha256_file(document),
            }
        )
        return result

    def validate_document(self, path: str, timeout_secs: float = 120) -> dict[str, Any]:
        document = self._document_path(path)
        return self._invoke("document.validate", {"document_path": str(document)}, timeout_secs)

    def save_copy(
        self,
        source_path: str,
        output_path: str,
        overwrite: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        source = self._document_path(source_path)
        output = self._output_path(output_path, {_DOCUMENT_SUFFIX})
        replaced_existing = output.exists()
        if replaced_existing and not overwrite:
            raise BridgeError("Output already exists; set overwrite=true to replace it")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output.stem, suffix=output.suffix, dir=str(output.parent)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        try:
            result = self._invoke(
                "document.save_copy",
                {"document_path": str(source), "output_path": str(staged)},
                timeout_secs,
            )
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD did not create the document copy")
            os.replace(str(staged), str(output))
            result = _replace_staged_path(result, staged, output)
            result.update(
                {
                    "source_path": str(source),
                    "output_path": str(output),
                    "bytes": output.stat().st_size,
                    "sha256": _sha256_file(output),
                    "overwritten": replaced_existing,
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()
            _remove_staged_backups(staged)

    def add_primitive(
        self,
        document_path: str,
        primitive: str,
        name: str,
        label: Optional[str] = None,
        dimensions: Optional[Mapping[str, float]] = None,
        translation: Optional[Sequence[float]] = None,
        rotation_axis: Optional[Sequence[float]] = None,
        rotation_degrees: float = 0,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.add_primitive",
            document_path,
            {
                "primitive": primitive,
                "name": self._object_name(name),
                "label": label,
                "dimensions": dict(dimensions or {}),
                "translation": list(translation or (0, 0, 0)),
                "rotation_axis": list(rotation_axis or (0, 0, 1)),
                "rotation_degrees": rotation_degrees,
            },
            timeout_secs,
        )

    def update_primitive(
        self,
        document_path: str,
        object_name: str,
        dimensions: Mapping[str, float],
        label: Optional[str] = None,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        if not dimensions:
            raise BridgeError("dimensions must contain at least one property")
        return self._mutate_document(
            "model.update_primitive",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "dimensions": dict(dimensions),
                "label": label,
            },
            timeout_secs,
        )

    def transform_object(
        self,
        document_path: str,
        object_name: str,
        translation: Sequence[float],
        rotation_axis: Sequence[float] = (0, 0, 1),
        rotation_degrees: float = 0,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.transform_object",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "translation": list(translation),
                "rotation_axis": list(rotation_axis),
                "rotation_degrees": rotation_degrees,
            },
            timeout_secs,
        )

    def boolean_operation(
        self,
        document_path: str,
        operation: str,
        base_object: str,
        tool_object: str,
        result_name: str,
        result_label: Optional[str] = None,
        timeout_secs: float = 300,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.boolean_operation",
            document_path,
            {
                "operation": operation,
                "base_object": self._object_name(base_object),
                "tool_object": self._object_name(tool_object),
                "result_name": self._object_name(result_name),
                "result_label": result_label,
            },
            timeout_secs,
        )

    def remove_object(
        self,
        document_path: str,
        object_name: str,
        cascade: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "document.remove_object",
            document_path,
            {"object_name": self._object_name(object_name), "cascade": bool(cascade)},
            timeout_secs,
        )

    def import_geometry(
        self,
        document_path: str,
        input_path: str,
        object_name: str,
        label: Optional[str] = None,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        source = self._input_path(input_path, _IMPORT_SUFFIXES)
        return self._mutate_document(
            "model.import_geometry",
            document_path,
            {
                "input_path": str(source),
                "object_name": self._object_name(object_name),
                "label": label,
            },
            timeout_secs,
        )

    def export_geometry(
        self,
        document_path: str,
        object_names: Sequence[str],
        output_path: str,
        linear_deflection: float = 0.1,
        angular_deflection_degrees: float = 15,
        overwrite: bool = False,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        if not object_names or len(object_names) > 100:
            raise BridgeError("object_names must contain between 1 and 100 names")
        names = [self._object_name(name) for name in object_names]
        output = self._output_path(output_path, _EXPORT_SUFFIXES)
        replaced_existing = output.exists()
        if replaced_existing and not overwrite:
            raise BridgeError("Output already exists; set overwrite=true to replace it")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output.stem, suffix=output.suffix, dir=str(output.parent)
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        try:
            result = self._invoke(
                "model.export_geometry",
                {
                    "document_path": str(document),
                    "object_names": names,
                    "output_path": str(staged),
                    "linear_deflection": linear_deflection,
                    "angular_deflection_degrees": angular_deflection_degrees,
                },
                timeout_secs,
            )
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("FreeCAD did not create a durable export")
            os.replace(str(staged), str(output))
            result = _replace_staged_path(result, staged, output)
            result.update(
                {
                    "document_path": str(document),
                    "output_path": str(output),
                    "bytes": output.stat().st_size,
                    "sha256": _sha256_file(output),
                    "overwritten": replaced_existing,
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()
            _remove_staged_backups(staged)


def get_bridge() -> FreecadBridge:
    return FreecadBridge.from_env()
