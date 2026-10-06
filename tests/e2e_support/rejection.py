"""Match a failed skill envelope to the observed real native shape rejection."""

from .client import canonical


def native_shape_rejection(result, observed, version):
    assert len(observed) == 1, "Expected exactly one observed native verification rejection"
    native = observed[0]
    payload = native["payload"]
    assert native["error_type"] == "WriteVerificationError"
    assert native["method"] == payload.get("tool") == "model.add_primitive"
    assert payload.get("host_version") == version
    check = payload.get("check")
    assert check in {"object.shape.not_null", "object.shape.valid"}, "Wrong native rejection stage"
    expected, actual = payload.get("expected"), payload.get("actual")
    if check == "object.shape.not_null":
        assert expected == "a non-null shape" and actual == "null"
    else:
        assert expected is True and actual is False
    params = payload.get("params", {})
    assert params.get("name") == "RejectedTinyBox" and params.get("primitive") == "box"
    assert canonical(params.get("dimensions")) == canonical(
        {"length": 1e-9, "width": 1e-9, "height": 1e-9}
    )
    assert result.get("success") is False and result.get("error") == "WriteVerificationError"
    assert result.get("context", {}).get("error_type") == "WriteVerificationError"
    details = result.get("_meta", {}).get("dcc.error", {})
    assert details.get("type") == "WriteVerificationError"
    assert isinstance(details.get("message"), str) and details["message"] == native["message"]
    # Only this allowlisted classification enters public evidence. The exception
    # and its native payload are observed without modification and re-raised.
    return {
        "error_type": "WriteVerificationError",
        "tool": payload["tool"],
        "check": check,
        "host_version": version,
        "native_error_matched_sdk_envelope": True,
    }
