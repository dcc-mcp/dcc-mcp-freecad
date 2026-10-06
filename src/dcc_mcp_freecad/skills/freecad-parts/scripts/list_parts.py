"""List the configured offline standard-parts library.

The listing runs in the adapter process, not in FreeCAD, so it stays available
when no host is installed: a caller can browse the library before it has a
document to insert into.
"""

from dcc_mcp_core.skill import run_main, skill_entry, skill_error, skill_success

from dcc_mcp_freecad.bridge import get_bridge
from dcc_mcp_freecad.parts_library import ENV_LIBRARY_ROOTS, PartLibraryError


@skill_entry
def main(category=None, query=None, limit=50, **kwargs):
    try:
        result = get_bridge().list_parts(category=category, query=query, limit=limit)
    except PartLibraryError as error:
        # A refusal to list is a configuration answer, not a FreeCAD failure:
        # name the variable and keep the code branchable.
        return skill_error(
            str(error),
            error.code,
            prompt=(
                "Set %s to one or more local directories that already hold the parts, "
                "then call list_parts again. The adapter is offline-only and never "
                "downloads a library." % ENV_LIBRARY_ROOTS
            ),
            possible_solutions=[
                "Point %s at an existing local directory." % ENV_LIBRARY_ROOTS,
                "Point %s at a local checkout of the FreeCAD parts library." % ENV_LIBRARY_ROOTS,
            ],
            **error.context(),
        )
    return skill_success(
        "Listed %d standard part(s) from the offline parts library." % result["returned"],
        prompt="Pass one of the returned `path` values to insert_part as part_ref.",
        verified=True,
        postcondition={"method": "library_directory_scan", "roots": result["roots"]},
        **result,
    )


if __name__ == "__main__":
    run_main(main)
