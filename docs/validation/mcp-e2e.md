# Real MCP and native-artifact CI

The reconciled CI requires 56 headless native cases and 38 GUI-persistence
cases on each SHA-verified official FreeCAD 1.0.2 and 1.1.4 AppImage. Those lanes
exercise the native bridge directly; current main's PartDesign coverage is retained.

The new `freecad_mcp` case adds the official MCP SDK transport: initialize and
catalog discovery, actual wire-name resolution, full source/load-skill schema
comparison, typed modeling, dependency recomputation after a dimension change,
a real native write rejection with source-byte rollback, presentation save/copy,
independent reopening, and raw owned process/API cleanup. Core 0.20.41 and MCP SDK
1.30.0 are pinned for this gate. Its helper accepts Core's existing tools/list
projection of only the save_copy root if/then; full source arguments remain
validated. It has no fallback to direct Python handlers.

The saved model is rendered by a separate repository-owned native test fixture.
That fixture uses FreeCAD's real view and Qt PNG codec, checks nonuniform pixels,
removes textual metadata, and verifies unchanged RGBA and saved state. A bounded
blue-color witness requires 8–65% image coverage, a connected region of at least
6% of pixels, and meaningful two-dimensional extent; blank, stray-pixel,
axis-only, neutral and scattered-noise controls must fail. Metrics remain in the
artifact so actual native results can be assessed without a pixel-equality
golden image. The thresholds still require real-host qualification. **This is
not a public MCP capture endpoint.** The public adapter still has no capture or
TechDraw tool. The CI PNG is evidence, not the authentic UI GIF planned under
[the demo recording plan](../demos/README.md).

On each supported host, CI selects exactly one real MCP case and rejects skips.
Xvfb/xcb and software rendering provide a real GUI context without a physical
screen. The existing official-download checksum verification and cache are
reused. No native mock is counted toward this result.

Artifacts contain a sanitized logical call trace, host/SDK versions, geometry
and rollback assertions, raw direct-child return codes, API cleanup results and
the native PNG. The allowlisted trace excludes native stdout/stderr, endpoints
and filesystem paths. Public JUnit retains test counts while removing hostnames,
captured output and exception details; the stage report identifies the failed
step. Native model files and raw process logs are not uploaded. A failure cannot
be reported as a native pass merely because a partial image or result exists.

Historical head `73bd3dcaa587479393676214f2b1400d1041e1ff` passed
[CI on 2026-10-08](https://github.com/dcc-mcp/dcc-mcp-freecad/actions/runs/37721312386):
41 headless, 38 GUI, one MCP and four FEM cases per host, with zero skips.
The combination with current main requires **56 headless, 38 GUI, one MCP and
four FEM cases per host** and has not yet received its own exact-revision result.
Pure schema, artifact-sanitization and cleanup tests are contract evidence only;
the historical run cannot qualify the newly reconciled candidate.

The negative mutation case must return a completed Core job with the intended
failed skill envelope. An observational test wrapper records the actual native
WriteVerificationError and immediately re-raises it unchanged. The SDK envelope
must match that exception's type and message; its native tool, invalid-shape
check, expected/actual comparison, host and tiny-box request are checked explicitly.
Only an allowlisted classification enters public evidence. Failed Core jobs,
timeouts, imports and unrelated verification checks cannot qualify as rollback.
