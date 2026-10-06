"""Synthetic evidence controls; none of these tests count as native execution."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from e2e_support.client import Client
from e2e_support.pixels import colored_geometry
from e2e_support.rejection import native_shape_rejection


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
