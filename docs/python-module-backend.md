# Explicit isolated native-library backend

The default remains FreeCADCmd. An operator may opt into a separate compatible Python interpreter that imports the installed native FreeCAD library. There is no automatic fallback after a CLI failure, no host upgrade, no arbitrary macro/tool argument, and no access to an existing GUI session. An explicit presentation-copy request initializes native GUI view providers only inside its isolated child process.

Set `DCC_MCP_FREECAD_BACKEND=python-module`, `DCC_MCP_FREECAD_PYTHON` to an explicit compatible interpreter, and `DCC_MCP_FREECAD_MODULE_DIRECTORY` to the installed directory containing FreeCAD.so / FreeCAD.pyd. Keep `DCC_MCP_FREECAD_ALLOWED_ROOTS` confined to the task's workspace. Library/interpreter paths are launch configuration, not MCP tool parameters. Compatibility depends on the native library's Python/compiler ABI; only the recorded Linux configuration is qualified here.

Each typed call starts a fresh interpreter with `-I`, a package-owned bootstrap and the unchanged method-whitelisted driver. User site/PYTHONPATH are ignored; HOME, XDG directories and FreeCAD's user-home override point into that call's temporary directory. The process is bounded by the existing timeout, response-size, workspace, source-size, staging and cancellation mechanisms. It cannot access a running GUI document.

Primary references: [FreeCAD's archived embedding documentation](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Embedding_FreeCAD.md) documents native-module imports and warns that compatible Python/compiler builds are required; [headless documentation](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Headless_FreeCAD.md) distinguishes App geometry from unavailable GUI view providers. These references support the operating mode, not portability claims about every binary build.

## Initial qualification

Linux, installed FreeCAD 1.0.0 native library, /usr/bin/python3 reporting Python 3.13.5; no application installation or upgrade. The ordinary freecadcmd route on this same host crashes during interpreter startup, so its failure is preserved separately rather than hidden.

138 unit/contract tests pass; three existing real-host CLI tests skip in this isolated suite. Through the official MCP SDK 1.30.0, Core/server 0.20.39 and 125 actual recorded protocol events, all 13 typed actions were exercised: status/capabilities, create, inspect, validate, add, update, transform, boolean, save-copy, import, export and remove. The proof changes a box to 24×10×4mm and cuts a radius-2mm bore. Native volume is 909.7345175425634mm³ versus analytic 909.7345175425633mm³. The full final Part::Cut readback survives save-copy/reopen exactly; STEP reimport returns the same volume. STEP and bounded STL are actual native exports. This proof is a software fixture, not a finished showcase.

The first actual primitive write exposed a pre-existing adapter/Core result-key collision: the native driver's `verified` list was passed to Core's boolean metadata argument. The write had already committed. A later actual inspect confirmed the object existed; the failed write was not blindly retried. The wrapper now preserves the list under `context.verified_checks` and emits boolean postcondition metadata separately, with regression tests. No native mutation or field verification is skipped.

The initial evidence above used Core/server 0.20.39. The final source qualification on current main passed 139 tests, with three existing native-CLI tests skipped, and repeated the 13-tool lifecycle on Core/server 0.20.41 with 128 recorded protocol events. See [the current qualification summary](validation/python-module-native.md) and its selected JSON evidence for those final results.

Source default behavior is unchanged; tests and native qualification do not establish macOS, Windows, other native Python ABIs, GUI workbenches, external plugins, or an application release.
