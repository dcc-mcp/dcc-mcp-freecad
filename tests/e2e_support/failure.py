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
