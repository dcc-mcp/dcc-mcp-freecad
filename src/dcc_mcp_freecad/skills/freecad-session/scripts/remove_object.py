from dcc_mcp_core.skill import run_main

from dcc_mcp_freecad.skill_tools import bridge_main

main = bridge_main("remove_object", "FreeCAD object removed.")

if __name__ == "__main__":
    run_main(main)
