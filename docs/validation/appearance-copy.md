# Appearance and framing qualification

This change adds bounded presentation controls to `save_copy`. It has source
and synthetic-test evidence, plus a limited Linux native qualification described
below. That run used separate bootstrap compatibility overlays and therefore
does not qualify the unmodified adapter or every supported host.

## Contract

`appearances` is an optional array of 1–1000 closed objects containing
`object_name`, `rgb` (exactly three finite numbers in `[0,1]`) and `opacity`
(a finite number in `[0,1]`). Names must be unique members of the explicit
`visible_objects` list and refer to existing non-null top-level Part features.
Unsupported or initially nonuniform face RGB/transparency is rejected before any
appearance or visibility mutation. This narrow profile does not support mesh,
container, body-member, per-face material or arbitrary material execution.

Native `Transparency` is an integer percentage, so the adapter quantizes
`100 * (1 - opacity)` to the nearest integer with ties upward and returns the
actual applied opacity. RGB channels are normalized to native 8-bit storage
before assignment: convert to float32, multiply by 255 in float32, round to the
nearest integer with ties upward, then divide by 255. This matches FreeCAD's
`Color::getPackedValue` and material-list persistence. RGB readback keeps the
existing strict `1e-6` absolute serialization tolerance;
integer transparency is checked exactly. Every native face material's RGB and
transparency must agree with the scalar provider values before saving and after
reopening. Other material channels, including specular, emission and shininess,
are not required to be uniform and are not changed by these controls.
The original requested RGB/opacity remains in `presentation_request.appearances`;
`presentation.appearances` contains actual applied values. For example, 0.9
becomes 230/255 while 0.12345 becomes 31/255. The tolerance is not widened to
accept an adjacent stored color. Diffuse alpha is not independently qualified
by this profile.

`frame_margin` is optional and finite in `[0,1]`. It increases fitted
orthographic camera height by `1 + 2 * frame_margin`, adding that fraction of
the fitted height at each side. It does not promise screen-pixel padding,
product occupancy, a capture aspect ratio, lighting, or rendered quality.
The node's height, serialized camera height and reopened camera must agree.

Both controls require `visible_objects`. The original file cannot be the
presentation destination, even with overwrite enabled. Existing no-overwrite
and cancellation publication safeguards remain in effect. Geometry identity
checks retain names, types, links, placements, topology counts and analytic
metrics; these are semantic recorded checks, not exact BRep/connectivity proof.
Before/after source SHA-256 comparisons in qualification tests prove unchanged
source bytes for those test runs. The runtime does not pin an expected input
hash, check source freshness, or provide compare-and-swap against a concurrently
changed source. Its contract is a distinct-output presentation copy. A saved
copy's whole-file hash is expected to differ because its presentation changed.

## Official source evidence

Source inspection covers FreeCAD tags **1.0.0, 1.0.2 and 1.1.4**. It establishes
API presence and implementation, not compatibility of a particular installed
binary, Python ABI, display or native persistence behavior.

- [1.0.0 ShapeColor compatibility getter/setter](https://github.com/FreeCAD/FreeCAD/blob/1.0.0/src/Gui/ViewProviderGeometryObjectPyImp.cpp): maps the alias to `ShapeAppearance` diffuse colors
- [1.0.0 native transparency property](https://github.com/FreeCAD/FreeCAD/blob/1.0.0/src/Gui/ViewProviderGeometryObject.cpp): integer percentage and material synchronization
- [1.0.0 material-list setters](https://github.com/FreeCAD/FreeCAD/blob/1.0.0/src/App/PropertyStandard.cpp): uniform diffuse color/transparency assignment
- [1.0.0 packed color semantics](https://github.com/FreeCAD/FreeCAD/blob/1.0.0/src/App/Color.cpp): float32 scaling, `std::lround`, and 8-bit restore
- [1.0.0 material-list save/restore](https://github.com/FreeCAD/FreeCAD/blob/1.0.0/src/App/PropertyStandard.cpp): `SaveDocFile` stores packed diffuse RGBA; restore decodes those packed channels
- [1.0.0 material Python properties](https://github.com/FreeCAD/FreeCAD/blob/1.0.0/src/App/MaterialPyImp.cpp): RGBA diffuse color and fractional transparency readback
- [1.0.0 camera Python API](https://github.com/FreeCAD/FreeCAD/blob/1.0.0/src/Gui/View3DPy.cpp): `fitAll`, `getCameraNode` and native Pivy camera node
- [1.0.2 camera API](https://github.com/FreeCAD/FreeCAD/blob/1.0.2/src/Gui/View3DPy.cpp) and [appearance aliases](https://github.com/FreeCAD/FreeCAD/blob/1.0.2/src/Gui/ViewProviderGeometryObjectPyImp.cpp)
- [1.1.4 camera API](https://github.com/FreeCAD/FreeCAD/blob/1.1.4/src/Gui/View3DPy.cpp), [appearance aliases](https://github.com/FreeCAD/FreeCAD/blob/1.1.4/src/Gui/ViewProviderGeometryObjectPyImp.cpp), and [material-list setters](https://github.com/FreeCAD/FreeCAD/blob/1.1.4/src/App/PropertyStandard.cpp)

## Native gate still required

1. On each pinned CI host, run the full existing real presentation suite plus
   `test_real_appearance_and_relative_frame_persist` (27 GUI cases total; the CI
   exact-count gate is updated accordingly). The new test creates only
   synthetic geometry, changes two parts, saves, reopens in a separate process,
   and independently checks face colors, opacity, unchanged unselected-part
   appearance, camera height, source bytes and recorded geometry.
2. Separately qualify the actual installed FreeCAD **1.0.0** runtime. The
   existing CI fixture intentionally pins **1.0.2/1.1.4** and cannot stand in
   for that runtime. Record exact adapter source revision, FreeCAD/Python
   versions, backend, GUI/Pivy availability, platform and display configuration.
3. For every intended product-copy selection, inspect current providers and
   confirm the required top-level/uniform-face-RGB-and-transparency profile before attempting
   a fresh non-overwriting copy. Check intended physical object identities,
   independent geometry summaries, source SHA-256, copy reopening, face values
   and margin readback. Preserve the source and any existing destination on
   invalid names, non-finite inputs, rejected providers, cancellation and
   readback failures.
4. Native capture/rendering is a separate gate: render the saved copy with the
   authorized capture implementation and inspect actual pixels at the delivery
   aspect ratio. Verify framing, color, transparent-part readability and native
   capture provenance before claiming visual quality. This patch neither adds
   nor qualifies a capture endpoint.

Do not publish a release or advertise native qualification solely on mock tests
or these source references.

## Limited Linux native qualification

A four-call sequence (`get_status`, `inspect_document`, `save_copy`, then
`inspect_document` after reopening) passed on FreeCAD **1.0.0** with Python
**3.13.5**, using the official MCP SDK and Core/server **0.20.41** through isolated
Python-module children. Each native child exited zero without a signal. Source
bytes, the 186 editable objects, 16 physical parts, object identities, types,
placements and recorded geometry remained unchanged. Requested and applied
RGB/opacity readbacks and an isometric `frame_margin` of 0.12 persisted after
save/reopen. These recorded geometry checks do not establish full BRep equivalence.

The run used two separately reviewed installed-resource bootstrap compatibility
overlays (`module_runner.py` and `module_resources.py`). Neither overlay is part
of this appearance patch. This evidence does not qualify an unmodified released
adapter, the default backend, public CI, other platforms or other host builds.
The pinned CI hosts still require their own acceptance on the public patch.
No image was captured or rendered, and visual quality is not claimed.

## Coin wrapper initialization

When `frame_margin` is supplied, the presentation helper explicitly imports
`pivy.coin` after validating selected providers and before changing their state.
FreeCAD 1.0.0 `getCameraNode()` converts its camera through the SWIG runtime table;
the conversion itself does not import the named Python module. Loading Coin
wrappers first registers that table. A missing or failing Pivy dependency stops
before provider mutation. Requests without `frame_margin` do not add this import.

This ordering fix does not establish runtime ABI compatibility. Native import,
camera access, save/reopen and the existing strict appearance/framing checks must
still pass on each qualified host.
