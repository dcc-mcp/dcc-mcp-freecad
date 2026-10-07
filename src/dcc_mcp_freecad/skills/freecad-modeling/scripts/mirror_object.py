from dcc_mcp_core.skill import run_main

from dcc_mcp_freecad.skill_tools import bridge_main

main = bridge_main("mirror_object", "FreeCAD object mirrored.")

if __name__ == "__main__":
    run_main(main)
