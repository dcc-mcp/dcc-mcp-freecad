"""Synthetic evidence controls; none of these tests count as native execution."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from e2e_support.client import Client
from e2e_support.pixels import colored_geometry
from e2e_support.rejection import native_shape_rejection


def test_native_output_classification_preserves_input_and_omits_private_text():
    import json

    from e2e_support.failure import native_output_summary

    result = {
        "stdout": "unrecognized private message /private/project.FCStd user@example.invalid",
        "stderr": (
            "Program received signal SIGSEGV, Segmentation fault.\n"
            "#0 0x123 in QWidget::~QWidget from /private/host/libQt6Widgets.so+0x55\n"
        ),
        "stderr_truncated": True,
    }
    before = deepcopy(result)
    output = native_output_summary(result)
    assert result == before
    assert output[0]["markers"] == output[0]["components"] == []
    assert output[1]["markers"] == ["freecad_sigsegv"]
    assert output[1]["components"] == ["libQt6Widgets.so", "QWidget::~QWidget"]
    assert output[1]["truncated"] is True
    assert output[0]["characters"] == len(result["stdout"])
    for private in ("private", "example.invalid", "0x123", "0x55", "project.FCStd"):
        assert private not in json.dumps(output)


@pytest.mark.parametrize("returncode", [1, -11])
def test_nonzero_native_exit_remains_nonzero_without_known_diagnostic_marker(returncode):
    from e2e_support.cleanup import close_owned
    from e2e_support.failure import native_output_summary

    handle = SimpleNamespace(signal_shutdown=lambda: None, shutdown=lambda: None)
    server = SimpleNamespace(stop=lambda: None, is_running=False)
    process = SimpleNamespace(poll=lambda: returncode, wait=lambda timeout: returncode)
    summary = native_output_summary({"stderr": "unknown native failure"})
    assert summary[1]["markers"] == []
    _, terminal = close_owned(server, handle, [("document.save_copy", process)])
    assert terminal[0]["returncode"] == returncode
    assert terminal[0]["cleanup_termination"] is False


def image(kind, width=128, height=128, blue=(51, 140, 204)):
    output = bytearray([255, 255, 255, 255] * (width * height))
    for y in range(height):
        for x in range(width):
            region = width // 8 <= x < 7 * width // 8 and height // 4 <= y < 3 * height // 4
            hole = (x - width / 2) ** 2 + (y - height / 2) ** 2 < (width / 12) ** 2
            selected = {
                "blank": False,
                "stray": x == width // 2 and y == height // 2,
                "axes": x == width // 2 or y == height // 2,
                "noise": x % 3 == 0 and y % 3 == 0,
                "blue_background": True,
                "gray_geometry": region and not hole,
                "legend": x < 8 and y < 8,
                "bracket": region and not hole,
            }[kind]
            if selected:
                color = (110, 110, 110) if kind == "gray_geometry" else blue
                index = (y * width + x) * 4
                output[index : index + 4] = bytes(color) + (255).to_bytes(1, "big")
    return bytes(output)


@pytest.mark.parametrize("blue", [(51, 140, 204), (30, 78, 120)])
def test_substantial_connected_shaded_blue_bracket_has_pixel_evidence(blue):
    result = colored_geometry(image("bracket", blue=blue), 128, 128)
    assert result["qualified"] is True
    assert 0.25 < result["coverage"] < 0.5
    assert result["largest_component_pixels"] == result["colored_pixels"]
    assert result["bounds_pixels"] == [16, 32, 112, 96]


@pytest.mark.parametrize(
    "kind", ["blank", "stray", "axes", "noise", "blue_background", "gray_geometry", "legend"]
)
def test_non_model_pixels_cannot_qualify_as_rendered_geometry(kind):
    with pytest.raises(AssertionError):
        colored_geometry(image(kind), 128, 128)


def rejection_pair(check="object.shape.not_null"):
    from dcc_mcp_core.skill import skill_exception

    from dcc_mcp_freecad.bridge import WriteVerificationError

    payload = {
        "tool": "model.add_primitive",
        "check": check,
        "host_version": "1.0.2",
        "expected": "a non-null shape" if check.endswith("not_null") else True,
        "actual": "null" if check.endswith("not_null") else False,
        "params": {
            "document_path": "/synthetic/private/stage.FCStd",
            "primitive": "box",
            "name": "RejectedTinyBox",
            "dimensions": {"length": 1e-9, "width": 1e-9, "height": 1e-9},
        },
    }
    error = WriteVerificationError(payload, "synthetic native shape rejection")
    envelope = skill_exception(error, include_traceback=False)
    observed = [
        {
            "method": "model.add_primitive",
            "error_type": "WriteVerificationError",
            "payload": payload,
            "message": str(error),
        }
    ]
    return envelope, observed


@pytest.mark.parametrize("check", ["object.shape.not_null", "object.shape.valid"])
def test_exact_native_rejection_matches_sdk_and_publishes_only_classification(check):
    envelope, observed = rejection_pair(check)
    before = deepcopy((envelope, observed))
    result = native_shape_rejection(envelope, observed, "1.0.2")
    assert result == {
        "error_type": "WriteVerificationError",
        "tool": "model.add_primitive",
        "check": check,
        "host_version": "1.0.2",
        "native_error_matched_sdk_envelope": True,
    }
    assert (envelope, observed) == before
    assert "private" not in str(result) and "message" not in result


@pytest.mark.parametrize(
    "fault",
    [
        "core_error",
        "sdk_type",
        "context_type",
        "missing_context",
        "message",
        "native_type",
        "wrong_method",
        "wrong_check",
        "wrong_host",
        "wrong_object",
        "wrong_dimensions",
        "wrong_comparison",
        "no_observation",
        "two_observations",
    ],
)
def test_unrelated_failure_cannot_be_called_native_rollback(fault):
    envelope, observed = rejection_pair()
    if fault == "core_error":
        envelope = {"status": "failed", "error": "ExecutorUnavailable"}
    elif fault == "sdk_type":
        envelope["error"] = "BridgeTimeoutError"
    elif fault == "context_type":
        envelope["context"]["error_type"] = "ImportError"
    elif fault == "missing_context":
        envelope.pop("context")
    elif fault == "message":
        envelope["_meta"]["dcc.error"]["message"] = "different unrelated failure"
    elif fault == "native_type":
        observed[0]["error_type"] = "BridgeError"
    elif fault == "wrong_method":
        observed[0]["method"] = "document.inspect"
    elif fault == "wrong_check":
        observed[0]["payload"]["check"] = "dimension.Length"
    elif fault == "wrong_host":
        observed[0]["payload"]["host_version"] = "1.1.4"
    elif fault == "wrong_object":
        observed[0]["payload"]["params"]["name"] = "OtherBox"
    elif fault == "wrong_dimensions":
        observed[0]["payload"]["params"]["dimensions"]["length"] = 1e-6
    elif fault == "wrong_comparison":
        observed[0]["payload"]["actual"] = "missing"
    elif fault == "no_observation":
        observed = []
    else:
        observed.append(deepcopy(observed[0]))
    with pytest.raises(AssertionError):
        native_shape_rejection(envelope, observed, "1.0.2")


@pytest.mark.parametrize("fault", ["failed_core_job", "malformed_result"])
def test_expected_native_error_does_not_allow_core_or_protocol_failure(fault):
    def response(value):
        return SimpleNamespace(isError=False, structuredContent=value)

    if fault == "failed_core_job":
        values = [
            {
                "core_job_id": "job",
                "job_id_owner": "core",
                "core_poll": {
                    "owner": "core",
                    "tool": "jobs_get_status",
                    "arguments": {"job_id": "job", "include_result": True},
                },
            },
            {
                "job_id": "job",
                "tool": "add_primitive",
                "status": "failed",
                "error": "ExecutorUnavailable",
            },
        ]
    else:
        values = [{"error": "unexpected malformed error"}]

    class Session:
        async def call_tool(self, *args, **kwargs):
            return response(values.pop(0))

    client = Client(
        Session(),
        {
            "add_primitive": (
                "add_primitive",
                "freecad_modeling__add_primitive",
                {"type": "object", "additionalProperties": False},
            )
        },
        lambda *args: None,
    )
    with pytest.raises(AssertionError):
        asyncio.run(client.call("add_primitive", {}, expect_success=False))


def test_native_failure_location_does_not_disclose_exception_text():
    from pathlib import Path

    from e2e_support.failure import native_image_failure, validate_failure

    root = Path(__file__).resolve().parents[1]
    try:
        colored_geometry(image("blank"), 128, 128)
    except AssertionError as error:
        details = native_image_failure(error, root)
    assert validate_failure(details) == details
    assert details["source"] == "tests/e2e_support/pixels.py" and details["line"] > 0
    try:
        raise ValueError("private /home/person/file token-like-value")
    except ValueError as error:
        details = native_image_failure(error, root)
    assert details == {"error_type": "ValueError", "source": None, "line": None}


def test_capture_diagnostic_re_raises_identical_error_without_rewriting_it(tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).parent / "native/mcp_capture_host.py"
    spec = importlib.util.spec_from_file_location("native_capture_diagnostic", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    error = ValueError("synthetic private diagnostic text")

    def fail(_params):
        raise error

    monkeypatch.setattr(module, "_capture_impl", fail)
    destination = tmp_path / "failure.json"
    with pytest.raises(ValueError) as result:
        module._capture({"failure_path": str(destination)})
    assert result.value is error and str(result.value) == "synthetic private diagnostic text"
    report = json.loads(destination.read_text())
    assert report["source"] == "tests/native/mcp_capture_host.py"
    assert "synthetic" not in destination.read_text() and "private" not in destination.read_text()


def test_native_failure_record_refuses_untrusted_detail_fields():
    from e2e_support.failure import validate_failure

    with pytest.raises(AssertionError):
        validate_failure(
            {"error_type": "ValueError", "source": None, "line": None, "message": "raw detail"}
        )
