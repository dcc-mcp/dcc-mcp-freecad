from dcc_mcp_core.skill import run_main

from dcc_mcp_freecad.skill_tools import snapshot_main

main = snapshot_main("delete_snapshot", "FreeCAD document snapshot deleted.")


if __name__ == "__main__":
    run_main(main)
