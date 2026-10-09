"""Report only repository-owned failure locations, never exception text/locals."""

import json
import re
from contextlib import contextmanager
from pathlib import Path

SOURCES = {
    "tests/native/mcp_capture_host.py",
    "tests/native/presentation_host.py",
    "tests/e2e_support/pixels.py",
}
KINDS = {
    "AssertionError",
    "AttributeError",
    "ImportError",
    "RuntimeError",
    "SystemError",
    "OSError",
    "TypeError",
    "ValueError",
}

NATIVE_MARKERS = {
    "freecad_sigsegv": "Program received signal SIGSEGV, Segmentation fault.",
    "freecad_unexpected_termination": "Application unexpectedly terminated",
    "python_fatal_error": "Fatal Python error:",
    "cpp_terminate": "terminate called",
    "x11_io_error": "XIO:",
    "x11_connection_broken": "The X11 connection broke",
    "xcb_display_unavailable": "could not connect to display",
    "qt_thread_timer": "Timers can only be used with threads started with QThread",
    "coin_missing_child": "tried to remove non-existent child",
}
NATIVE_COMPONENTS = (
    "libFreeCADGui.so",
    "libFreeCADApp.so",
    "libFreeCADBase.so",
    "libCoin.so",
    "libQt5Core.so",
    "libQt5Gui.so",
    "libQt5Widgets.so",
    "libQt6Core.so",
    "libQt6Gui.so",
    "libQt6Widgets.so",
    "libpython3.11.so",
    "Py_FinalizeEx",
    "QApplication::~QApplication",
    "QObject::~QObject",
    "QWidget::~QWidget",
    "Gui::Application::~Application",
    "App::Application::destruct",
)

STACK_LIBRARIES = {
    "libFreeCADGui.so": r"Gui::",
    "libFreeCADApp.so": r"App::",
    "libFreeCADBase.so": r"Base::",
    "libQt5Core.so": r"Q[A-Z][A-Za-z0-9_]*::",
    "libQt5Gui.so": r"Q[A-Z][A-Za-z0-9_]*::",
    "libQt5Widgets.so": r"Q[A-Z][A-Za-z0-9_]*::",
    "libQt6Core.so": r"Q[A-Z][A-Za-z0-9_]*::",
    "libQt6Gui.so": r"Q[A-Z][A-Za-z0-9_]*::",
    "libQt6Widgets.so": r"Q[A-Z][A-Za-z0-9_]*::",
    "libpython3.11.so": r"_?Py",
}


def public_backtrace(value):
    """Keep public-library function identifiers, never arguments or host paths."""
    frames = []
    for line in value.splitlines()[:512]:
        if not re.match(r"^#\d{1,3}\s", line):
            continue
        for library, prefix in STACK_LIBRARIES.items():
            if not re.search(r"/" + re.escape(library) + r"(?:\.\d+)*[+(]", line):
                continue
            match = re.search(r"\bin ([A-Za-z_~][A-Za-z0-9_:~]{0,159})\(", line)
            symbol = match.group(1) if match and re.match(prefix, match.group(1)) else None
            frames.append({"library": library, "symbol": symbol})
            break
        if len(frames) == 32:
            break
    return frames


def native_exception_output(error, invoke_code):
    """Observe bytes already read by this exact bridge frame; no stream reads."""
    trace = error.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code is invoke_code:
            values = trace.tb_frame.f_locals
            if all(isinstance(values.get(key), str) for key in ("stdout", "stderr")):
                bounded = {}
                for key in ("stdout", "stderr"):
                    bounded[key] = values[key][:65536]
                    bounded[key + "_truncated"] = len(values[key]) > 65536
                return native_output_summary(bounded)
            break
        trace = trace.tb_next
    return []


def native_result_summary(path):
    """Read only the bounded result file before the existing context removes it."""
    if not path.is_file():
        return {"state": "absent"}
    with path.open("rb") as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        return {"state": "oversized"}
    try:
        value = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeError):
        return {"state": "invalid_json"}
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        return {"state": "invalid_shape"}
    if value["ok"]:
        return {"state": "ok"}
    error = value.get("error")
    if not isinstance(error, dict):
        return {"state": "invalid_shape"}

    def classification(item):
        if not isinstance(item, dict):
            return None
        kind, message = item.get("type"), item.get("message")
        return {
            "type": kind if isinstance(kind, str) and kind in KINDS else "OtherException",
            "cleanup_marker": {
                "Native GUI teardown found an open document": "open_document",
                "Native GUI refused to close its main window": "close_refused",
                "Native GUI main window deletion did not complete": "delete_incomplete",
            }.get(message)
            if isinstance(message, str)
            else None,
        }

    return {
        "state": "error",
        "primary": classification(error),
        "cleanup": classification(error.get("gui_cleanup_error")),
    }


@contextmanager
def observed_result_directory(factory, pending, records, *args, **kwargs):
    """Observe an owned result at normal finalization; retain original cleanup."""
    with factory(*args, **kwargs) as directory:
        try:
            yield directory
        finally:
            child = pending.pop(str(Path(directory)), None)
            if child is not None:
                number, method, process = child
                row = {"child_number": number, "method": method, "returncode": process.returncode}
                try:
                    row["result"] = (
                        {"state": "not_terminal"}
                        if process.returncode is None
                        else native_result_summary(Path(directory) / "result.json")
                    )
                except Exception as error:
                    kind = type(error).__name__
                    row["result"] = {
                        "state": "observation_error",
                        "error_type": kind if kind in KINDS else "OtherException",
                    }
                records.append(row)


def native_output_summary(result):
    """Classify bounded native output without exporting text, paths or addresses.

    A missing marker means unknown, never a successful or harmless exit.
    The actual result and raw process return code remain untouched.
    """
    output = []
    for stream in ("stdout", "stderr"):
        value = result.get(stream, "")
        assert isinstance(value, str) and len(value) <= 65536
        output.append(
            {
                "stream": stream,
                "characters": len(value),
                "truncated": bool(result.get(stream + "_truncated", False)),
                "markers": sorted(name for name, text in NATIVE_MARKERS.items() if text in value),
                "components": [name for name in NATIVE_COMPONENTS if name in value],
                "backtrace": public_backtrace(value),
            }
        )
    return output


def native_image_failure(error, root):
    result = {
        "error_type": type(error).__name__ if type(error).__name__ in KINDS else "OtherException",
        "source": None,
        "line": None,
    }
    trace = error.__traceback__
    while trace is not None:
        try:
            relative = (
                Path(trace.tb_frame.f_code.co_filename).resolve().relative_to(root).as_posix()
            )
        except ValueError:
            relative = None
        if relative in SOURCES:
            result.update(source=relative, line=trace.tb_lineno)
        trace = trace.tb_next
    return result


def validate_failure(value):
    assert set(value) == {"error_type", "source", "line"}
    assert value["error_type"] in KINDS | {"OtherException"}
    assert value["source"] is None or value["source"] in SOURCES
    assert value["line"] is None or type(value["line"]) is int and 0 < value["line"] < 10000
    return value
