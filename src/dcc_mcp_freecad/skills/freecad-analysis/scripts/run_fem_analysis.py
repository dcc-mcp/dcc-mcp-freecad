from dcc_mcp_core.skill import run_main

from dcc_mcp_freecad.skill_tools import bridge_main

main = bridge_main("run_fem_analysis", "FreeCAD FEM analysis solved.")

if __name__ == "__main__":
    run_main(main)
