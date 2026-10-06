from __future__ import annotations

from typing import Any, Callable

from dcc_mcp_core.skill import skill_entry, skill_success

from .bridge import get_bridge


def bridge_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        result = getattr(get_bridge(), method)(**kwargs)
        # Core's verified keyword is boolean postcondition metadata, not the
        # native driver's list of completed checks. Keep that list in context.
        checks = result.get("verified")
        if isinstance(checks, list):
            context = dict(result)
            context["verified_checks"] = context.pop("verified")
            return skill_success(
                message,
                verified=bool(checks),
                postcondition={"method": "native_document_readback", "checks": checks},
                **context,
            )
        return skill_success(message, **result)

    return main
