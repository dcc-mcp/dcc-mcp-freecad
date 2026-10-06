"""Report only repository-owned failure locations, never exception text/locals."""

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
