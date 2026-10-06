# Headless render qualification

`render_view` renders an FCStd document view to a PNG in an isolated
`FreeCADCmd` process. This note records what is proven, by which evidence, and
what is explicitly not claimed.

## Why the verification exists

A screenshot tool that cannot tell a real frame from a broken one is worse than
no screenshot tool: the agent reasons about a picture that contains nothing and
the failure is silent. Two independent defect classes have to be refused, and
they need different detectors.

### Defect 1: the capture never reached the scene

A flat fill -- all black, all white, one colour. Cheap to detect: one luminance
level, a near-zero spread, or a single colour covering essentially every pixel.

### Defect 2: the background rendered, the model did not

This is the one that defeats a naive check. **FreeCAD's default 3D-view
background is a linear gradient** (`View3DInventorViewer` sets
`LinearGradient` on construction; `View3DSettings` defaults `Gradient` to
`true`), and `savePicture` explicitly re-adds the gradient node to the render
root, so the gradient is **baked into the saved PNG**. An entirely empty render
therefore has plenty of pixel variance and passes any "is it monochrome?"
test on 1.0.2 and 1.1.4 alike.

The adapter refuses this by capturing **twice**: once as requested, and once
with every object hidden at the identical camera. The second frame is what the
scene looks like with nothing selected, so anything the pair has in common is
background. A subject that does not differ from it by at least 0.5% of its
pixels rendered no geometry.

Counting pixels that differ from the empty-scene reference is also a
"non-background pixel share" measurement that needs no knowledge of what the
background actually is -- it works whether the host renders a gradient, a flat
colour, or a user-configured one.

| Check | Error code | Threshold |
| --- | --- | --- |
| flat fill | `degenerate_render` | luminance levels >= 2, stddev >= 1.0, dominant colour <= 0.999 of the frame |
| no geometry | `empty_render` | pixels differing from the empty scene by >= 8/255 per channel, >= 0.5% of the frame |

Both failures carry the full pixel summary, both captures' statistics, the
measured fraction, the reasons, and remediation. Neither returns an image, and
because the verdict runs before publication neither can reach `output_path`.

## Read-only guarantee

Framing the scene means moving the camera and changing visibility, so the
native camera, visibility and selection are recorded before anything is touched
and restored afterwards. The restore happens in a `finally`, so it runs even
when the capture itself fails.

The restoration is itself a read-back check (`render.view_state_restored`), and
it is **byte-exact, with no tolerance**: `getCamera()` round-trips byte for
byte through `setCamera()` on the supported hosts, so anything less than
equality is a real difference in where the view now points. Both snapshots are
returned as `view_state_before` and `view_state_after` so the claim is visible
to the caller and assertable in a test rather than resting on a comment.

This matters even though every call runs in a fresh process. The guarantee is a
property of the tool, not of the current deployment: it must still hold if the
driver is ever run against a long-lived host, and `zoom`/`set_camera`-style
callers must be able to trust that a look does not move their view.

The document is opened, recomputed and mutated only in memory. It is never
saved, so the source file is byte-for-byte unchanged -- asserted on real
hardware after every test.

## Host API facts this relies on

Verified against FreeCAD source at tags `1.0.2` and `1.1.4`:

- `view.saveImage` is registered as `METH_VARARGS` and parsed with
  `PyArg_ParseTuple`. **Keyword arguments raise `TypeError`; there is no
  keyword spelling.** The signature is
  `saveImage(filename, width, height, color, comment, samples)`. The adapter
  calls it positionally and takes the defaults (`"Current"` keeps the
  document's own background; `"$MIBA"` records the camera matrix as PNG
  provenance).
- The default `SavePicture` backend is `SoQtOffscreenRenderer`, which creates
  its own `QOpenGLContext` and `QOffscreenSurface` and therefore does not need
  a shown window. The preference is deliberately left unset so this default
  stays in effect; the `CoinOffscreenRenderer` and framebuffer backends are
  worse headless choices.
- `FreeCADGui.OffscreenRenderer` does not exist. `coin.SoOffscreenRenderer`
  does, but it needs X11/GLX, has no PNG writer, and omits camera and lights
  from `getSceneGraph()` -- which is the usual origin of "black image" reports.
  Neither is used.
- Offscreen rendering failure raises (`Base::RuntimeError("Offscreen rendering
  failed")`) rather than writing a blank PNG, so a hard failure is already
  visible. The pixel check catches the softer case where a file is written but
  carries no signal.

Two defensive measures come from the same source review:

- The notification area is disabled before `FreeCADGui` is imported. Under the
  Qt offscreen platform it can self-deadlock: showing a notification calls
  `raise()`, the unsupported `raise()` is logged to FreeCAD's console, and the
  console handler re-enters the notification area on the same thread.
- `LIBGL_ALWAYS_SOFTWARE=1` is set on the child process (overridable by the
  operator) so Mesa uses a software rasteriser on hosts with no GPU.

## Evidence

| Layer | Where | What it proves |
| --- | --- | --- |
| PNG decoding | `tests/test_raster.py` | all five scanline filters round-trip; corrupt, truncated, paletted, interlaced and oversized files are refused; greyscale, RGB, grey+alpha and RGBA flatten correctly |
| Verdict logic | `tests/test_raster.py` | flat fills, near-flat frames, fully transparent frames and gradient-only frames are refused; real signals pass; the distinct-colour cap does not flip a verdict |
| Geometry detection | `tests/test_raster.py` | a measured 4% region passes, a 0.03% sliver is refused, sub-threshold channel deltas are ignored |
| Adapter contract | `tests/test_render_view.py` | bounds, refuse-before-publication, inline image limits, `output_path` rules, capability reporting, and the host capture sequence |
| View-state restore | `tests/test_render_view.py` | the state is restored, restored on failure, restored with the pre-render snapshot, and a state that does not come back is refused |
| Real hardware | `tests/test_real_render.py` | four view presets on **1.0.2 and 1.1.4**; maximum capture size; source byte-identical after every call; byte-exact camera round-trip; capability `available` on both hosts |
| Real refusal path | `tests/test_real_render.py` | the host's own bytes corrupted after a genuine capture produce `degenerate_render` and `empty_render` end to end, with nothing published |

`tests/test_real_render.py` is selected by `-m freecad_gui` and runs in the
`freecad-real` CI job on both pinned AppImages. That job asserts an exact,
non-skipped test count, so a host that cannot render at all fails the build
rather than quietly dropping the suite.

## Not claimed

- **No claim that every host can render.** A console-only FreeCAD build has no
  `FreeCADGui` and is reported as `host_limited` by `get_capabilities` with
  remediation, rather than silently returning nothing.
- **No claim about image quality.** The verdict is close to "obviously broken",
  not "looks good". A frame that passes is non-degenerate and contains the
  requested geometry; it may still be badly lit or awkwardly framed.
- **The 0.5% geometry threshold is a judgement, not a measurement of
  correctness.** It is chosen so a thin sliver still passes while
  anti-aliasing noise does not. The measured fraction is always returned so a
  caller can see how close it came and a future change can re-tune it against
  real data rather than by guesswork.
- **Which blank-frame cause dominates on a given host is not assumed.** The
  two detectors are independent, so the adapter does not need to know whether a
  failure came from a paint race or from framing; it reports the measurement
  and the caller can tell from the statistics which one it was.
- Offscreen rendering under `QT_QPA_PLATFORM=offscreen` is what this adapter
  uses and what CI exercises. FreeCAD's own upstream CI uses `xvfb-run`; if a
  host cannot render at all under offscreen, Xvfb is the better-supported
  fallback and `LIBGL_ALWAYS_SOFTWARE=1` remains the first thing to try.
