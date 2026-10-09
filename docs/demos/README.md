# Agent-driven FreeCAD UI demo: recording plan

The planned subject is an original pneumatic-cylinder cutaway. The translucent
barrel exposes an orange piston, steel rod and dark seal rings; blue end caps,
four tie rods with visible heads, and brass air fittings show how the parts fit.
The agent will change the piston position by 12 mm through typed MCP calls.
This is a geometric demonstration, not a fluid simulation, kinematic joint or
manufacturing-approved design.

The official FreeCAD feature showcase uses layered, colored mechanical geometry
and visible native dimensions to make the model understandable. The official
[0.20 release notes](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Release_notes_0.20.md)
also show animated native feature examples, including pocket-direction changes.
These are references for clarity and pacing; none of their pixels or geometry
will be used in this demo. The original recipe is [actuator_scene.py](actuator_scene.py).
It only emits a plan and never launches FreeCAD or calls MCP.

## What the recording must show

Use supported CUA captures/recording for this take; no alternate X11-grab route.
Capture the real FreeCAD application window continuously, keeping its model tree,
property editor and viewport legible. The current adapter performs file-based
operations in standalone native children. The recording will therefore show
FreeCAD opening each actual saved MCP result, with this behavior stated in the
caption. It must not imply that the adapter currently offers live GUI editing.
No rendered-PNG slideshow, simulated application UI or copied official media.

Suggested edit, 20–24 seconds at approximately 960×600:

1. **0–3 s:** Show the hollow barrel and end caps. Expand the real Boolean tree;
   selecting BarrelBore shows Radius=13.5 mm in FreeCAD's property editor
2. **3–7 s:** Open the agent-produced mechanism checkpoint. The transparent barrel
   reveals piston, rod and seals. Select the real Piston feature
3. **7–12 s:** Open the assembled checkpoint. Show tie rods, heads and both bored
   air fittings. Briefly orbit using the actual viewport to reveal depth
4. **12–18 s:** Show the agent's typed transform operations and reopen the saved
   stroke checkpoint. Piston, rod and seals move together by 12 mm; select the
   native placement property so the change is visible
5. **18–24 s:** Hold the final model, readable tree and native validation result

The compact on-video caption can say: “Agent → typed MCP → FreeCAD. Saved results
reopened in the UI; waiting time shortened.” It must describe the actual take.
Maintain a timestamp-to-call/checkpoint map alongside the original screen recording
and successful SDK receipts. Speed changes and removed waiting periods must be
recorded; no frame may present an unexecuted operation as completed.

## Recording and delivery gate

Each phase must be separately bound and reviewed before native execution. The
recipe needs native shape/readback qualification; it is not an executed project.
After each phase, validate and inspect actual solids, save a new presentation,
and bind its file hash. Keep source project checkpoints and the original UI video.
Use only the coordinator's scheduled GUI slot and close all owned native apps
normally afterward. No recording is performed by this source preparation.

Before publishing, inspect every frame for user names, local paths, notification
banners, network addresses and secrets. Record into a clean session and synthetic
project; do not obscure defects with a crop. Keep any unavoidable private master
outside the repository. Export a small GIF (target under 4 MiB) from the actual
UI footage, plus an MP4 with accessible captions. A README image/link is added
only after those files exist, pass visual review and have verified public links.
Suggested alt text: “An agent uses typed MCP commands to build a pneumatic-cylinder
cutaway in FreeCAD, then moves its piston and rod by 12 millimetres; the native
model tree and placement values are visible.”

Sketch constraints/dimension annotations and TechDraw are not present in the
current typed adapter. This demonstration does not simulate those capabilities.
TechDraw remains tracked separately in
[issue 35](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/35).
