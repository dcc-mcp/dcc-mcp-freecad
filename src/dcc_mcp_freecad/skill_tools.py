from __future__ import annotations

from typing import Any, Callable, Mapping

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from .bridge import get_bridge
from .snapshots import SnapshotError


def _success(message: str, postcondition_method: str, result: dict[str, Any]) -> dict[str, Any]:
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
        return _success(message, "native_document_readback", result)

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
        return _success(message, "snapshot_readback", result)

    return main
