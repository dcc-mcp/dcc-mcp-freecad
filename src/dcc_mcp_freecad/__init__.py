from .bridge import FemAnalysisError, SketchStateError, WriteVerificationError
from .parts_library import PartLibraryError
from .server import FreecadMcpServer
from .sketch_rules import SketchSpecError
from .write_contract import MUTATING_TOOLS, READ_ONLY_TOOLS, SERVICE_READ_ONLY_TOOLS

# ``SketchStateError`` is deliberately the bridge's, not ``sketch_rules'``: the
# name exists in both modules with different constructor signatures. The
# sketch_rules one is raised inside the FreeCAD process; the bridge one is what
# the caller actually catches after it crosses the boundary, and it is a
# BridgeError so an existing ``except BridgeError`` handler keeps working.
__all__ = [
    "FemAnalysisError",
    "FreecadMcpServer",
    "PartLibraryError",
    "SketchSpecError",
    "SketchStateError",
    "WriteVerificationError",
    "MUTATING_TOOLS",
    "READ_ONLY_TOOLS",
    "SERVICE_READ_ONLY_TOOLS",
]
