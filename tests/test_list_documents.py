"""Document discovery under the allowed roots.

``list_documents`` is the adapter's only view of "what is on disk", so these
tests pin the two things that make it safe to hand to an agent: every bound
holds (entries, bytes, scanned entries, bytes read), and a path it reports is a
path the rest of the tool surface accepts.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from dcc_mcp_freecad import bridge as bridge_module
from dcc_mcp_freecad.bridge import AllowedRootsError, BridgeError, FreecadBridge


class FakeFreecad(FreecadBridge):
    """A bridge that answers native calls without a FreeCAD process."""

    def __init__(self, *roots: Path):
        super().__init__(allowed_roots=list(roots))
        self.executable = "fake-freecadcmd"

    def _invoke(self, method, params, timeout_secs=120):
        if method == "document.inspect":
            return {
                "file_name": params.get("document_path"),
                "object_count": 0,
                "objects": [],
            }
        return {"object_count": 0}


def _write(root: Path, name: str, payload: bytes = b"fcstd") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _archive(root: Path, name: str, document_xml: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("Document.xml", document_xml)
    return path


_OBJECTS_XML = b"""<?xml version='1.0' encoding='utf-8'?>
<Document SchemaVersion="4" ProgramVersion="1.0.0" FileVersion="1">
  <Objects Count="2">
    <Object name="Body" type="Part::Box" />
    <Object name="Port" type="Part::Cylinder" />
  </Objects>
  <ObjectData Count="2">
    <Object name="Body" />
    <Object name="Port" />
  </ObjectData>
</Document>
"""


def test_listing_reports_relative_and_absolute_paths(tmp_path: Path):
    root = tmp_path / "allowed"
    _write(root, "projects/enclosure.FCStd", b"enclosure")

    listing = FakeFreecad(root).list_documents()

    assert listing["returned"] == 1
    entry = listing["entries"][0]
    assert entry["path"] == "projects/enclosure.FCStd"
    assert entry["root"] == str(root.resolve())
    assert entry["absolute_path"] == str((root / "projects/enclosure.FCStd").resolve())
    assert entry["size_bytes"] == len(b"enclosure")
    assert entry["modified_at"]
    assert entry["sha256"] == hashlib.sha256(b"enclosure").hexdigest()
    assert entry["sha256_scope"] == "full"


def test_listed_paths_satisfy_the_document_path_contract(tmp_path: Path):
    """A listed path must be a path inspect_document accepts, not a lookalike."""
    root = tmp_path / "allowed"
    _write(root, "projects/enclosure.FCStd", b"enclosure")
    _write(root, "nested/deeper/bracket.FCStd", b"bracket")
    bridge = FakeFreecad(root)

    listing = bridge.list_documents()
    assert listing["returned"] == 2
    for entry in listing["entries"]:
        assert bridge._document_path(entry["absolute_path"]) == Path(entry["absolute_path"])
        assert (Path(entry["root"]) / entry["path"]).resolve() == Path(entry["absolute_path"])
        bridge.inspect_document(entry["absolute_path"])


def test_listing_is_sorted_stably_across_pages(tmp_path: Path):
    root = tmp_path / "allowed"
    for name in ("c.FCStd", "a.FCStd", "b.FCStd"):
        _write(root, name)
    bridge = FakeFreecad(root)

    names = [entry["path"] for entry in bridge.list_documents(limit=200)["entries"]]
    assert names == ["a.FCStd", "b.FCStd", "c.FCStd"]


def test_root_outside_the_allowed_roots_is_refused(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside, "secret.FCStd")

    with pytest.raises(AllowedRootsError, match="outside DCC_MCP_FREECAD_ALLOWED_ROOTS"):
        FakeFreecad(allowed).list_documents(root=str(outside))


def test_refusal_is_still_a_bridge_error_for_existing_handlers(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(BridgeError):
        FakeFreecad(tmp_path / "allowed").list_documents(root=str(outside))


def test_missing_root_is_refused_without_scanning(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    with pytest.raises(BridgeError, match="not an existing directory"):
        FakeFreecad(allowed).list_documents(root=str(allowed / "nope"))


def test_omitted_root_searches_every_allowed_root(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "a.FCStd")
    _write(second, "b.FCStd")

    listing = FakeFreecad(first, second).list_documents()

    assert [entry["path"] for entry in listing["entries"]] == ["a.FCStd", "b.FCStd"]
    assert listing["roots"] == [str(first.resolve()), str(second.resolve())]


def test_empty_directory_returns_an_empty_page(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()

    listing = FakeFreecad(root).list_documents()

    assert listing["entries"] == []
    assert listing["returned"] == 0
    assert listing["truncated"] is False
    assert listing["next_offset"] is None


def test_paging_advances_over_a_stable_cursor(tmp_path: Path):
    root = tmp_path / "allowed"
    for index in range(5):
        _write(root, "doc-%d.FCStd" % index)
    bridge = FakeFreecad(root)

    first = bridge.list_documents(limit=2)
    assert [entry["path"] for entry in first["entries"]] == ["doc-0.FCStd", "doc-1.FCStd"]
    assert first["truncated"] is True
    assert first["next_offset"] == 2

    second = bridge.list_documents(limit=2, offset=first["next_offset"])
    assert [entry["path"] for entry in second["entries"]] == ["doc-2.FCStd", "doc-3.FCStd"]
    assert second["truncated"] is True
    assert second["next_offset"] == 4

    third = bridge.list_documents(limit=2, offset=second["next_offset"])
    assert [entry["path"] for entry in third["entries"]] == ["doc-4.FCStd"]
    assert third["truncated"] is False
    assert third["next_offset"] is None


def test_offset_past_the_end_returns_an_empty_page(tmp_path: Path):
    root = tmp_path / "allowed"
    _write(root, "only.FCStd")

    listing = FakeFreecad(root).list_documents(offset=50)

    assert listing["entries"] == []
    assert listing["truncated"] is False
    assert listing["next_offset"] is None


def test_limit_and_offset_are_bounded(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    bridge = FakeFreecad(root)

    with pytest.raises(BridgeError, match="limit must be between 1 and 200"):
        bridge.list_documents(limit=0)
    with pytest.raises(BridgeError, match="limit must be between 1 and 200"):
        bridge.list_documents(limit=201)
    with pytest.raises(BridgeError, match="offset must be between"):
        bridge.list_documents(offset=-1)


def test_oversized_result_is_truncated_with_a_cursor(tmp_path: Path):
    root = tmp_path / "allowed"
    for index in range(250):
        _write(root, "doc-%03d.FCStd" % index)

    listing = FakeFreecad(root).list_documents(limit=200)

    assert listing["returned"] == 200
    assert listing["truncated"] is True
    assert listing["next_offset"] == 200

    follow = FakeFreecad(root).list_documents(limit=200, offset=listing["next_offset"])
    assert follow["returned"] == 50
    assert follow["truncated"] is False
    assert follow["entries"][0]["path"] == "doc-200.FCStd"
    assert follow["entries"][-1]["path"] == "doc-249.FCStd"


def test_response_byte_budget_truncates_the_page(tmp_path: Path, monkeypatch):
    root = tmp_path / "allowed"
    for index in range(20):
        _write(root, "doc-%02d.FCStd" % index)
    monkeypatch.setattr(bridge_module, "_MAX_LIST_BYTES", 512)

    listing = FakeFreecad(root).list_documents(limit=200)

    assert listing["returned"] < 20
    assert listing["truncated"] is True
    # The page must always advance, however small the budget.
    assert listing["next_offset"] == listing["returned"]
    assert listing["next_offset"] >= 1


def test_scan_budget_stops_the_walk_without_a_cursor(tmp_path: Path, monkeypatch):
    """An exhausted scan must not offer a cursor that walks the same ground."""
    root = tmp_path / "allowed"
    for index in range(200):
        _write(root, "doc-%03d.FCStd" % index)
    monkeypatch.setattr(bridge_module, "_MAX_LIST_SCAN_ENTRIES", 25)

    listing = FakeFreecad(root).list_documents()

    assert listing["scan_budget_exhausted"] is True
    assert listing["truncated"] is True
    assert listing["next_offset"] is None
    assert listing["scanned_entries"] == 25


def test_non_recursive_listing_skips_subdirectories(tmp_path: Path):
    root = tmp_path / "allowed"
    _write(root, "top.FCStd")
    _write(root, "nested/deep.FCStd")

    listing = FakeFreecad(root).list_documents(recursive=False)

    assert [entry["path"] for entry in listing["entries"]] == ["top.FCStd"]


def test_pattern_is_matched_case_insensitively_against_the_name_only(tmp_path: Path):
    root = tmp_path / "allowed"
    _write(root, "Bracket.fcstd")
    _write(root, "enclosure.FCStd")
    _write(root, "nested/deep.FCStd")
    bridge = FakeFreecad(root)

    assert [e["path"] for e in bridge.list_documents(pattern="*.FCStd")["entries"]] == [
        "Bracket.fcstd",
        "enclosure.FCStd",
        "nested/deep.FCStd",
    ]
    assert [e["path"] for e in bridge.list_documents(pattern="bracket.*")["entries"]] == [
        "Bracket.fcstd"
    ]


def test_path_patterns_are_refused(tmp_path: Path):
    root = tmp_path / "allowed"
    _write(root, "nested/deep.FCStd")
    bridge = FakeFreecad(root)

    for pattern in ("nested/*.FCStd", "..", "./*.FCStd"):
        with pytest.raises(BridgeError, match="single file-name glob"):
            bridge.list_documents(pattern=pattern)


def test_empty_and_oversized_patterns_are_refused(tmp_path: Path):
    bridge = FakeFreecad(tmp_path / "allowed")

    with pytest.raises(BridgeError, match="non-empty string"):
        bridge.list_documents(pattern="")
    with pytest.raises(BridgeError, match="at most 128 characters"):
        bridge.list_documents(pattern="a" * 129)


def test_symlinks_that_escape_the_root_are_not_listed(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _write(outside, "escape.FCStd", b"secret")
    link = allowed / "linked.FCStd"
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks are unavailable in this environment")

    listing = FakeFreecad(allowed).list_documents()

    assert listing["entries"] == [], "a link out of the root would name an unreachable path"


def test_object_count_is_opt_in(tmp_path: Path):
    root = tmp_path / "allowed"
    _archive(root, "two.FCStd", _OBJECTS_XML)

    default = FakeFreecad(root).list_documents()
    requested = FakeFreecad(root).list_documents(include_object_count=True)

    assert default["entries"][0]["object_count"] is None
    assert requested["entries"][0]["object_count"] == 2


def test_object_count_is_null_when_the_index_cannot_be_read(tmp_path: Path):
    root = tmp_path / "allowed"
    _write(root, "not-an-archive.FCStd", b"plain bytes")
    _archive(root, "no-index.FCStd", b"<Document></Document>")

    listing = FakeFreecad(root).list_documents(include_object_count=True)

    assert [entry["object_count"] for entry in listing["entries"]] == [None, None]


def test_object_count_uses_the_declared_count_without_named_children(tmp_path: Path):
    root = tmp_path / "allowed"
    _archive(
        root,
        "declared.FCStd",
        b'<Document><Objects Count="3"></Objects><ObjectData Count="3"/></Document>',
    )

    listing = FakeFreecad(root).list_documents(include_object_count=True)

    assert listing["entries"][0]["object_count"] == 3


def test_large_documents_are_digested_as_a_bounded_prefix(tmp_path: Path, monkeypatch):
    root = tmp_path / "allowed"
    payload = b"x" * 4096
    _write(root, "big.FCStd", payload)
    monkeypatch.setattr(bridge_module, "_MAX_LIST_HASH_BYTES_PER_FILE", 64)

    listing = FakeFreecad(root).list_documents()

    entry = listing["entries"][0]
    assert entry["sha256"] == hashlib.sha256(payload[:64]).hexdigest()
    assert entry["sha256_scope"] == "prefix"
    assert entry["size_bytes"] == 4096


def test_exhausted_read_budget_withholds_digests(tmp_path: Path, monkeypatch):
    root = tmp_path / "allowed"
    for index in range(10):
        _write(root, "doc-%d.FCStd" % index, b"y" * 32)
    monkeypatch.setattr(bridge_module, "_MAX_LIST_READ_BYTES", 64)

    listing = FakeFreecad(root).list_documents()

    assert listing["read_budget_exhausted"] is True
    scopes = {entry["sha256_scope"] for entry in listing["entries"]}
    assert scopes == {"full", "unavailable"}
    assert any(entry["sha256"] is None for entry in listing["entries"])


def test_listing_never_touches_file_contents(tmp_path: Path):
    """Discovery must not open, recompute, or rewrite a document."""
    root = tmp_path / "allowed"
    document = _archive(root, "untouched.FCStd", _OBJECTS_XML)
    before = document.read_bytes()
    bridge = FakeFreecad(root)
    invoked = []
    bridge._invoke = lambda method, params, timeout_secs=120: invoked.append(method) or {}

    listing = bridge.list_documents(include_object_count=True)

    assert listing["entries"][0]["object_count"] == 2
    assert invoked == [], "a listing must not dispatch a native call"
    assert document.read_bytes() == before


def test_listing_works_without_a_configured_freecad_host(tmp_path: Path):
    """Discovery is pure filesystem work, so a missing host must not block it."""
    root = tmp_path / "allowed"
    _write(root, "found.FCStd")
    bridge = FreecadBridge(allowed_roots=[root])
    bridge.executable = None

    assert bridge.list_documents()["returned"] == 1


def test_entry_bytes_stay_within_the_documented_budget(tmp_path: Path):
    root = tmp_path / "allowed"
    for index in range(200):
        _write(root, "doc-%03d.FCStd" % index)

    listing = FakeFreecad(root).list_documents(limit=200)

    assert len(json.dumps(listing["entries"], ensure_ascii=False)) <= bridge_module._MAX_LIST_BYTES


@pytest.mark.freecad
@pytest.mark.skipif(
    not os.environ.get("FREECAD_TEST_EXECUTABLE"), reason="FREECAD_TEST_EXECUTABLE is not set"
)
def test_real_freecad_object_count_matches_inspect_document(tmp_path: Path):
    """Pin the archive read against the host's own object count.

    The count is read from the archive rather than from FreeCAD, so this is the
    only place the assumption is checked against a real document. It runs on
    every real CI leg (1.0.x and 1.1.x).
    """
    executable = os.environ["FREECAD_TEST_EXECUTABLE"]
    bridge = FreecadBridge(executable, allowed_roots=[tmp_path])
    document = tmp_path / "counted.FCStd"
    bridge.create_document(str(document))
    bridge.add_primitive(str(document), "box", "Body", dimensions={"length": 20})
    bridge.add_primitive(str(document), "cylinder", "Port", dimensions={"radius": 4, "height": 8})

    inspected = bridge.inspect_document(str(document))
    listing = bridge.list_documents(include_object_count=True)

    assert listing["returned"] == 1
    entry = listing["entries"][0]
    assert entry["object_count"] == inspected["object_count"]
    assert entry["object_count"] == 2
    assert entry["sha256"] == bridge.inspect_document(str(document))["document_sha256"]
    assert entry["sha256_scope"] == "full"
    # The listed path is the path the host just opened.
    assert Path(entry["absolute_path"]).resolve() == document.resolve()
