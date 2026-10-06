"""Insert a standard part from the configured offline parts library.

The reference is containment-checked by the bridge before the document is
staged, so a refused ``part_ref`` costs no FreeCAD process and cannot leave a
half-applied mutation behind. The insert itself is proven by the same read-back
contract as every other mutating tool.
"""

from dcc_mcp_core.skill import run_main, skill_entry, skill_error

from dcc_mcp_freecad.bridge import get_bridge
from dcc_mcp_freecad.parts_library import PartLibraryError
from dcc_mcp_freecad.skill_tools import bridge_success


@skill_entry
def main(document_path=None, part_ref=None, object_name=None, **kwargs):
    try:
        result = get_bridge().insert_part(
            document_path=document_path,
            part_ref=part_ref,
            object_name=object_name,
            label=kwargs.get("label"),
            translation=kwargs.get("translation") or (0, 0, 0),
            rotation_axis=kwargs.get("rotation_axis") or (0, 0, 1),
            rotation_degrees=kwargs.get("rotation_degrees") or 0,
            timeout_secs=kwargs.get("timeout_secs") or 600,
        )
    except PartLibraryError as error:
        # Refused before anything was read. Name the code so the caller can
        # branch, and point it back at the only legal source of a part_ref.
        return skill_error(
            str(error),
            error.code,
            prompt=(
                "Call list_parts and pass one of its returned `path` values as part_ref; "
                "the reference is refused before any file is read."
            ),
            possible_solutions=[
                "Call list_parts to obtain a valid relative part_ref.",
                "Configure %s if the library is not set up yet."
                % error.details.get("env_var", "DCC_MCP_FREECAD_PARTS_LIBRARY"),
            ],
            **error.context(),
        )
    return bridge_success(
        result,
        "Inserted standard part %r as %s." % (result.get("part_ref"), object_name),
    )


if __name__ == "__main__":
    run_main(main)
