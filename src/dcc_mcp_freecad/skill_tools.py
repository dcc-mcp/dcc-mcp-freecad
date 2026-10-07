from __future__ import annotations

from typing import Any, Callable

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from .bridge import BridgeTimeoutError, get_bridge
from .snapshots import SnapshotError


def bridge_success(
    message: str, postcondition_method: str, result: dict[str, Any]
) -> dict[str, Any]:
    checks = result.get("verified")
    if isinstance(checks, list):
        context = dict(result)
        context["verified_checks"] = context.pop("verified")
        return skill_success(
            message,
            verified=bool(checks),
            postcondition={"method": postcondition_method, "checks": checks},
            **context,
        )
    return skill_success(message, **result)


def bridge_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        result = getattr(get_bridge(), method)(**kwargs)
        # Core's verified keyword is boolean postcondition metadata, not the
        # native driver's list of completed checks. Keep that list in context.
        return bridge_success(message, "native_document_readback", result)

    return main


def snapshot_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    """Bridge entry for the snapshot tools, which refuse with structured codes.

    A snapshot refusal is a decision the caller can act on -- delete something,
    re-read the document, retry -- so the store's ``error_code`` becomes the
    result's error code and its remediation list becomes ``possible_solutions``
    instead of being flattened into one generic failure string.
    """

    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        try:
            result = getattr(get_bridge(), method)(**kwargs)
        except SnapshotError as error:
            return skill_error(
                str(error),
                error.error_code,
                prompt=(
                    error.remediation[0]
                    if error.remediation
                    else "Review the reported values and retry."
                ),
                possible_solutions=list(error.remediation) or None,
                **error.details,
            )
        return bridge_success(message, "snapshot_readback", result)

    return main


def script_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    """Bridge entry for ``run_script``, which can fail by exceeding its deadline.

    A timeout is the one script outcome that must not travel as a success. The
    wrapper cannot simply return the partial result with ``timed_out: True``
    either, because ``bridge_success`` reports success unconditionally - so a
    caller that ignored the flag would be told the script finished. It is
    therefore returned as an error carrying the partial payload, which keeps
    ``timed_out`` both programmatic (an ``error_code``, not prose to match) and
    accompanied by what the script printed before it was killed.

    A non-zero exit is *not* an error: the script ran to completion and decided
    to fail, and that is the caller's business to interpret from ``exit_code``.
    """

    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        try:
            result = getattr(get_bridge(), method)(**kwargs)
        except BridgeTimeoutError as error:
            return skill_error(
                str(error),
                "script_timeout",
                prompt=(
                    "The script exceeded its timeout and was terminated. Read stdout for how "
                    "far it got, or raise timeout_secs if the work genuinely needs longer."
                ),
                possible_solutions=[
                    "Read stdout in this result to see how far the script got.",
                    "Raise timeout_secs, up to DCC_MCP_FREECAD_MAX_SCRIPT_TIMEOUT_SECS.",
                    "Check for a modal dialog or prompt the script is blocked on.",
                ],
                **error.partial,
            )
        return bridge_success(message, "script_exit_code", result)

    return main
