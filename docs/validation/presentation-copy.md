# Native presentation-copy qualification

`save_copy` can persist an explicit final-object visibility selection and a
standard orthographic camera in a separate editable FCStd file. This requires
native FreeCAD GUI view providers. It provides no raster-rendering or visible
GUI acceptance.

## Source snapshot

The complete 16,961-byte source patch was received in two verified UTF-8 parts.
Its SHA-256 is
`76e23d507387b02af68befe97da7596842ccf51ccb3665a7e784de1048cdccf4`.
Applying it to public main `aedecd0eca640f914f2870aa2eb575d0fa81b8a1`
reproduced the supplied source tree
`917a8f1268a9efe21f75b4bdbf13037dbdc6777b` exactly. The cloud candidate is
`8e84c6e3ad0f49390e6cf5a5dba2071abd8ea360`; inherited version 0.5.0 is
package metadata, not a release of this change.

The source qualification summary records 142 baseline tests and 153 candidate
tests, with three existing native-CLI skips in each run. Lint, format, builds,
Twine and installed-wheel entry-point smoke passed. Its installed-wheel
qualification used Core/server/CLI 0.20.41, explicit `gateway_port=0` and
`enable_gateway_failover=False`, Linux FreeCAD 1.0.0 and native Python 3.13.5.
The summary records 172 actual MCP events, all four standard views, four fresh
native reopens, three editable objects and a final solid volume of
909.7345175425634 mm3, matching the analytic result within 1e-9 mm3. Eleven
negative cases preserved source and pre-existing destination bytes, and owned
staging/server cleanup passed. Native binaries and raw host logs are not part
of this publication.

That source snapshot precedes the publication review fixes below. It does not
qualify the final implementation or other native platforms/ABIs.

## Publication review

The final implementation compares the first native readback against the actual
request before saving: exact selected names, Orthographic type and native
quaternion orientation. Preset quaternions come from the pinned FreeCAD
[1.0.2 Camera implementation](https://github.com/FreeCAD/FreeCAD/blob/1.0.2/src/Gui/Camera.cpp)
and [1.1.4 Camera implementation](https://github.com/FreeCAD/FreeCAD/blob/1.1.4/src/Gui/Camera.cpp).
Rotations are normalized and accept equivalent opposite-sign quaternions at
1e-6 component tolerance. Reopened position, clipping, aspect, focal distance
and height are compared at 1e-6 relative/absolute tolerance; orientation uses
native rotation equivalence rather than serialized axis-angle spelling.

Selections must contain top-level non-container objects. Groups, LinkGroups, Parts, Bodies
and their members are rejected before changing provider state; local visibility
cannot establish visibility through hidden parents or cascading groups. The
public schema also requires explicit selection for nondefault views and bounds
object names consistently with the runtime.

Source replacement is rejected in presentation mode even with explicit
overwrite. Cancellation is checked before publication. No-overwrite copies
publish the completed sibling native file through an exclusive hard link, so
a destination created during native saving is preserved. This requires output
filesystem hard-link support; explicit overwrite uses atomic replacement.
Size, SHA-256 and response metadata are obtained from the verified stage before
publication. Cleanup of the owned stage and native backups is best effort:
filesystem cleanup errors preserve the committed result or original failure,
and can leave an owned temporary file for the operator to remove.
All files are read/written through native FreeCAD APIs, without rewriting FCStd
XML or binary contents externally. Geometry checks cover object inventory,
links, recorded placements, topology counts and aggregate metrics/analytic
bounds at 1e-10 relative and 1e-9 absolute tolerance. They do not establish full
BRep or mesh-connectivity equivalence.

## Final validation gate

Windows Python 3.12 and Core/server/CLI 0.20.41 passed the complete contract
selector used by CI: 176 passed, one platform-specific skip and 29 native cases
deselected, with one existing Install SOP deprecation warning. Local FreeCAD is
unavailable, so this result provides no native GUI acceptance.

The existing SHA-verified AppImage matrix retains its three native Cmd tests
on FreeCAD 1.0.2 and 1.1.4. A separate `freecad_gui` selector adds 26 real native
cases using the same production bridge and fresh isolated native processes:
all four views, relocated independent reopen, requested visibility/quaternion
and geometry readback, unknown selections, actual post-apply camera/visibility
faults, Group/LinkGroup/Part/Body rejection, source aliases, existing destinations and
unselected views. Both selectors have separate JUnit reports and mandatory
exact-count and zero-skip checks. Missing GUI support fails the selected GUI lane.

Exact-head CI must pass both native selectors and all contract/package jobs,
and complete review must have no unresolved substantive findings before merge.
Wheel/sdist byte identity, Python 3.7 grammar, entry-point smoke and package
payload/owner privacy inspection are final publication gates. No release or
package publication is performed by this task.
