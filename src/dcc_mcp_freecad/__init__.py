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
#
# ``SketchSpecError`` is exported for the local path, and only for the local
# path. ``sketch_rules`` is pure Python and importable on its own, so a caller
# can validate a geometry or constraint spec before spending a host round-trip
# and catch exactly this type. It carries none of the three attributes the
# driver forwards across the process boundary (``code``, ``payload``,
# ``state_payload``), so the *same* class raised inside the FreeCAD process
# reaches the caller as a plain ``BridgeError`` with the message and no code --
# ``except SketchSpecError`` does not fire on it. See docs/write-contract.md.
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
