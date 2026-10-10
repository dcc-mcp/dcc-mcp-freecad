from dcc_mcp_core.skill import run_main

from dcc_mcp_freecad.skill_tools import bridge_main

main = bridge_main("sweep_feature", "FreeCAD sweep feature created.")

if __name__ == "__main__":
    run_main(main)
