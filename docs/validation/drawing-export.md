# TechDraw drawing-page qualification

`create_drawing_page` builds a bounded TechDraw page with one view per requested
direction, and `export_drawing` renders it to PDF or SVG. This report records
the real-hardware run that qualifies both, on the two pinned release lines.

## What was run

Both legs install the pinned official FreeCAD AppImage (SHA-256 verified) and
run the suite against it. The console lane runs
`pytest -m "freecad and not freecad_gui"`; the offscreen GUI lane runs
`pytest -m freecad_gui` with `QT_QPA_PLATFORM=offscreen`.

| Host | Console lane | GUI lane |
| --- | --- | --- |
| FreeCAD 1.0.2 | 6 passed, 0 skipped | 30 passed, 0 skipped |
| FreeCAD 1.1.4 | 6 passed, 0 skipped | 30 passed, 0 skipped |

Each drawing case builds its own synthetic document: an 84 x 50 x 24 mm box with
a cylinder cut through it, documented as a three-view page (isometric, front,
top) on the built-in A4 landscape template.

## What the acceptance asserts

The export cases do not stop at "the call returned". For the PDF they assert the
artefact is non-empty, that it begins with the `%PDF-` magic bytes, and that it
parses back to exactly one page -- the page count is read out of the file twice,
once inside the host process and once again in the test process. For the SVG
they assert it parses as an XML document whose root element is
`{http://www.w3.org/2000/svg}svg`.

Page creation asserts, per view, the object type, the wired sources, the
projection direction, the scale, the page position, and that the direction
actually projected geometry. All of it is read back after the document is saved.

## Two host behaviours the real run caught

Neither is visible from the API documentation, and neither is reproducible
without a host, which is the argument for the real-hardware lane.

**`DrawPage::addView` re-centres a new view.** Any view with no owner is moved
to the middle of the page when it joins, and its `ScaleType` is dropped back to
`Automatic` if it does not fit as first added. A page built by setting `X` and
`Y` and then adding the view therefore comes out with every view stacked in the
middle. The layout is now applied after the view joins the page, and
`LockPosition` is set so a later recompute cannot move it either.

**`TechDraw.viewPartAsSvg` is not safe in a console process on 1.0.x.** It
dereferences the view's geometry object, which is not built headless, and 1.0.x
has no null check: the call segfaults the host instead of reporting anything.
1.1.x added the guard and returns an empty string, so the call was wrong on both
lines, just quietly on one and fatally on the other. The projection read-back
now uses `TechDraw.project`, the same hidden line removal taken straight from
the source compound and the view direction, with no view cache that can be
missing.

## GUI scope

Rendering needs the page's view provider and `PagePrinter`, both in
`TechDrawGui`, so `export_drawing` starts the FreeCAD GUI inside the isolated
process the bridge already owns. The GUI is started before the document is
opened, because `Gui.showMainWindow()` does not back-fill GUI documents for
files that were already open.

No GUI state outlives a call and nothing is screenshotted, so this path stays
orthogonal to the GUI-screenshot crash class reported against TechDraw
elsewhere. Page creation itself uses only the App-side `TechDraw` module and
needs no GUI at all.
