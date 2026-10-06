"""Offline parts library: listing and, above all, path containment.

The comparable FreeCAD MCP project joined a caller-supplied ``relative_path``
onto its library root with no containment check (their issue #121), which let a
caller read any file the service user could reach. Every case in this file is a
refusal that must happen *before* a file is opened, and every one carries a
stable error code the caller can branch on.

Two escape tests are deliberate:

* a real symlink, which is how an escape actually happens in production;
* a monkeypatched ``realpath``, which is how the same refusal is pinned on a
  runner that is not allowed to create symlinks (Windows without Developer
  Mode). Skipping the first is a runner limitation; the second always runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dcc_mcp_freecad import parts_library  # noqa: E402


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    (root / "fasteners" / "bolts").mkdir(parents=True)
    (root / "fasteners" / "nuts").mkdir(parents=True)
    (root / "bearings").mkdir(parents=True)
    (root / "fasteners" / "bolts" / "iso4014-m8x40.step").write_bytes(b"ISO-10303-21;")
    (root / "fasteners" / "nuts" / "iso4032-m8.step").write_bytes(b"ISO-10303-21;")
    (root / "bearings" / "6004-2rs.step").write_bytes(b"ISO-10303-21;")
    (root / "loose-plate.step").write_bytes(b"ISO-10303-21;")
    # Neither a part format nor a hidden library file belongs in the listing.
    (root / "fasteners" / "readme.txt").write_bytes(b"not a part")
    (root / ".git").mkdir()
    (root / ".git" / "config.step").write_bytes(b"ISO-10303-21;")
    return root


def roots(library: Path):
    return [library.resolve()]


# ---------------------------------------------------------------------------
# Library configuration
# ---------------------------------------------------------------------------


def test_unconfigured_library_is_refused_with_the_env_var_named():
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.parse_library_roots("")

    error = excinfo.value
    assert error.code == parts_library.LIBRARY_UNAVAILABLE
    assert parts_library.ENV_LIBRARY_ROOTS in str(error)
    assert "download" in str(error), "the refusal must say why there is nothing to list"


def test_missing_root_directory_is_refused(library: Path, tmp_path: Path):
    missing = tmp_path / "does-not-exist"

    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.parse_library_roots(str(missing))

    assert excinfo.value.code == parts_library.ROOT_MISSING
    assert str(missing.resolve()) in str(excinfo.value)


@pytest.mark.parametrize(
    "entry",
    [
        "https://example.com/parts",
        "http://example.com/parts",
        "git://example.com/parts.git",
        "file://example.com/parts",
    ],
)
def test_remote_sources_are_refused_instead_of_fetched(entry: str):
    """Offline-first: a URL is never turned into a download."""
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.parse_library_roots(entry)

    assert excinfo.value.code == parts_library.REMOTE_UNSUPPORTED
    assert "offline" in str(excinfo.value)


def test_one_bad_root_refuses_the_whole_configuration(library: Path):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.parse_library_roots(
            os.pathsep.join([str(library), "https://example.com/parts"])
        )

    assert excinfo.value.code == parts_library.REMOTE_UNSUPPORTED


def test_multiple_roots_are_resolved_and_deduplicated(library: Path, tmp_path: Path):
    second = tmp_path / "second-library"
    second.mkdir()
    parsed = parts_library.parse_library_roots(
        os.pathsep.join([str(library), str(second), str(library)])
    )

    assert parsed == [library.resolve(), second.resolve()]


def test_environment_is_the_source_of_truth(library: Path, monkeypatch):
    monkeypatch.setenv(parts_library.ENV_LIBRARY_ROOTS, str(library))

    assert parts_library.resolve_library_roots() == [library.resolve()]

    monkeypatch.delenv(parts_library.ENV_LIBRARY_ROOTS)
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_library_roots()
    assert excinfo.value.code == parts_library.LIBRARY_UNAVAILABLE


def test_error_payload_carries_the_code_and_details(library: Path):
    error = parts_library.PartLibraryError("some_code", "it broke", part_ref="a/b.step")

    payload = error.payload()

    assert payload["error_code"] == "some_code"
    assert payload["message"] == "it broke"
    assert payload["part_ref"] == "a/b.step"
    assert payload["schema_version"] == parts_library.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_listing_returns_only_part_formats_and_skips_hidden_directories(library: Path):
    result = parts_library.list_parts(roots=roots(library))

    assert [part["path"] for part in result["parts"]] == [
        "bearings/6004-2rs.step",
        "fasteners/bolts/iso4014-m8x40.step",
        "fasteners/nuts/iso4032-m8.step",
        "loose-plate.step",
    ]
    assert result["offline_only"] is True
    assert result["remote_sources"] is False
    assert result["truncated"] is False
    assert result["scan_limited"] is False


def test_listing_entry_shape_matches_what_insert_part_accepts(library: Path):
    part = next(item for item in parts_library.list_parts(roots=roots(library))["parts"])

    assert set(part) == {"name", "category", "path", "format", "size_bytes", "root"}
    assert part["name"] == "6004-2rs"
    assert part["category"] == "bearings"
    assert part["format"] == "step"
    assert part["size_bytes"] > 0
    # The listing's `path` is exactly the part_ref insert_part accepts.
    assert parts_library.resolve_part_ref(part["path"], roots(library))[0].is_file()


def test_root_level_parts_are_listed_with_an_empty_category(library: Path):
    result = parts_library.list_parts(query="loose", roots=roots(library))

    assert [part["category"] for part in result["parts"]] == [""]


def test_category_filter_matches_nested_directories(library: Path):
    result = parts_library.list_parts(category="fasteners", roots=roots(library))

    assert [part["path"] for part in result["parts"]] == [
        "fasteners/bolts/iso4014-m8x40.step",
        "fasteners/nuts/iso4032-m8.step",
    ]


def test_category_filter_is_case_insensitive_and_prefix_scoped(library: Path):
    result = parts_library.list_parts(category="Fasteners/Bolts", roots=roots(library))

    assert [part["path"] for part in result["parts"]] == ["fasteners/bolts/iso4014-m8x40.step"]


def test_query_is_a_case_insensitive_substring_of_the_relative_path(library: Path):
    result = parts_library.list_parts(query="M8", roots=roots(library))

    assert [part["path"] for part in result["parts"]] == [
        "fasteners/bolts/iso4014-m8x40.step",
        "fasteners/nuts/iso4032-m8.step",
    ]


def test_combined_filters_narrow_the_result(library: Path):
    result = parts_library.list_parts(category="fasteners", query="nut", roots=roots(library))

    assert [part["path"] for part in result["parts"]] == ["fasteners/nuts/iso4032-m8.step"]


def test_limit_is_bounded_and_reports_truncation(library: Path):
    result = parts_library.list_parts(limit=2, roots=roots(library))

    assert result["returned"] == 2
    assert result["total_matched"] == 4
    assert result["truncated"] is True


@pytest.mark.parametrize("limit", [0, -1, 501, "50", True, 1.5])
def test_out_of_range_and_non_integer_limits_are_refused(library: Path, limit):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.list_parts(limit=limit, roots=roots(library))

    assert excinfo.value.code == parts_library.INVALID_LIMIT


def test_integral_float_limit_is_accepted(library: Path):
    result = parts_library.list_parts(limit=1.0, roots=roots(library))

    assert result["returned"] == 1


def test_non_string_filters_are_refused(library: Path):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.list_parts(category=["fasteners"], roots=roots(library))

    assert excinfo.value.code == parts_library.INVALID_FILTER


def test_categories_cover_the_whole_library_not_just_the_page(library: Path):
    result = parts_library.list_parts(limit=1, roots=roots(library))

    assert result["categories"] == ["", "bearings", "fasteners"]


def test_scan_limit_is_reported(tmp_path: Path, monkeypatch):
    root = tmp_path / "huge"
    root.mkdir()
    for index in range(10):
        (root / ("part-%02d.step" % index)).write_bytes(b"ISO-10303-21;")
    monkeypatch.setattr(parts_library, "MAX_WALK_FILES", 4)

    result = parts_library.list_parts(roots=[root.resolve()])

    assert result["scan_limited"] is True
    assert result["returned"] <= 4


def test_library_roots_are_not_required_to_be_a_single_directory_tree(library: Path, tmp_path):
    second = tmp_path / "second-library"
    (second / "profiles").mkdir(parents=True)
    (second / "profiles" / "t-slot.step").write_bytes(b"ISO-10303-21;")

    result = parts_library.list_parts(roots=[library.resolve(), second.resolve()])

    assert "profiles/t-slot.step" in [part["path"] for part in result["parts"]]
    assert len(result["roots"]) == 2


# ---------------------------------------------------------------------------
# Path containment -- the refusals #121 was missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "part_ref",
    [
        "../outside.step",
        "fasteners/../../outside.step",
        "fasteners/bolts/../../../outside.step",
        "..",
        "../",
        "fasteners/./bolts/iso4014-m8x40.step",
        "/abs/path/part.step",
        "//server/share/part.step",
        "\\\\server\\share\\part.step",
        "C:\\library\\part.step",
        "c:/library/part.step",
        "D:part.step",
        "https://example.com/part.step",
        "http://example.com/part.step",
    ],
)
def test_traversal_absolute_and_remote_references_are_refused(library: Path, part_ref: str):
    """Every rejection happens from the string, before a file is opened."""
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref(part_ref, roots(library))

    assert excinfo.value.code in (
        parts_library.INVALID_PART_REF,
        parts_library.REMOTE_UNSUPPORTED,
    ), part_ref
    assert excinfo.value.details["part_ref"] == part_ref


def test_backslash_separators_are_refused_even_for_an_existing_part(library: Path):
    """A Windows-style spelling of a real part is still not a part_ref."""
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref("fasteners\\bolts\\iso4014-m8x40.step", roots(library))

    assert excinfo.value.code == parts_library.INVALID_PART_REF


def test_a_real_file_outside_the_library_stays_unreachable(library: Path, tmp_path: Path):
    """The point of the whole check: the file exists and is still refused."""
    outside = tmp_path / "outside.step"
    outside.write_bytes(b"ISO-10303-21;")

    for part_ref in (str(outside), "../outside.step"):
        with pytest.raises(parts_library.PartLibraryError) as excinfo:
            parts_library.resolve_part_ref(part_ref, roots(library))
        assert excinfo.value.code == parts_library.INVALID_PART_REF


@pytest.mark.parametrize("part_ref", ["", "   ", None, 17, ["a.step"]])
def test_empty_and_non_string_references_are_refused(library: Path, part_ref):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref(part_ref, roots(library))

    assert excinfo.value.code == parts_library.INVALID_PART_REF


def test_null_byte_is_refused(library: Path):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref("fasteners/bolts\0/iso4014-m8x40.step", roots(library))

    assert excinfo.value.code == parts_library.INVALID_PART_REF


@pytest.mark.parametrize("part_ref", ["readme.txt", "no-suffix", "archive.zip", "part.FCStd"])
def test_unsupported_formats_are_refused(library: Path, part_ref: str):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref(part_ref, roots(library))

    assert excinfo.value.code == parts_library.UNSUPPORTED_FORMAT
    assert "step" in str(excinfo.value)


def test_trailing_slash_is_refused(library: Path):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref("fasteners/bolts/", roots(library))

    assert excinfo.value.code == parts_library.INVALID_PART_REF


def test_missing_part_is_not_found_rather_than_escaped(library: Path):
    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref("fasteners/bolts/absent.step", roots(library))

    assert excinfo.value.code == parts_library.PART_NOT_FOUND
    assert excinfo.value.details["roots"] == [str(library.resolve())]


def test_a_symlink_out_of_the_library_is_refused(library: Path, tmp_path: Path):
    """Textual containment is not containment: prove it after ``realpath``."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.step").write_bytes(b"ISO-10303-21;")
    _symlink(outside_dir, library / "linked")

    # The link is not a part the library owns, so listing must not offer it.
    assert "linked/secret.step" not in [
        part["path"] for part in parts_library.list_parts(roots=roots(library))["parts"]
    ]

    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref("linked/secret.step", roots(library))

    assert excinfo.value.code == parts_library.ESCAPES_LIBRARY
    assert excinfo.value.details["part_ref"] == "linked/secret.step"


def test_escape_is_refused_even_where_symlinks_cannot_be_created(
    library: Path, tmp_path, monkeypatch
):
    """Deterministic on every runner: simulate the escape instead of making one.

    A runner without symlink privileges must not become a runner without this
    assertion, so the same refusal is pinned against a stubbed ``realpath``.
    """
    outside = (tmp_path / "elsewhere" / "secret.step").resolve()
    monkeypatch.setattr(
        parts_library,
        "_realpath",
        lambda value: str(outside) if value.endswith("secret.step") else value,
    )

    with pytest.raises(parts_library.PartLibraryError) as excinfo:
        parts_library.resolve_part_ref("fasteners/secret.step", roots(library))

    assert excinfo.value.code == parts_library.ESCAPES_LIBRARY
    assert str(outside).lower() in [item.lower() for item in excinfo.value.details["resolved"]]


def test_containment_check_refuses_a_sibling_directory():
    assert parts_library._contains("/library", "/library/part.step")
    assert parts_library._contains("/library", "/library/sub/part.step")
    assert not parts_library._contains("/library", "/library-two/part.step")
    assert not parts_library._contains("/library", "/elsewhere/part.step")
    assert parts_library._contains("/library", "/library")


def test_resolution_returns_the_file_and_its_root(library: Path):
    path, root = parts_library.resolve_part_ref(
        "fasteners/bolts/iso4014-m8x40.step", roots(library)
    )

    assert path.is_file()
    assert path.resolve().parent == (library / "fasteners" / "bolts").resolve()
    assert root == library.resolve()


def test_resolution_prefers_the_first_configured_root(library: Path, tmp_path: Path):
    second = tmp_path / "second-library"
    (second / "fasteners" / "bolts").mkdir(parents=True)
    (second / "fasteners" / "bolts" / "iso4014-m8x40.step").write_bytes(b"ISO-10303-21;")

    _path, root = parts_library.resolve_part_ref(
        "fasteners/bolts/iso4014-m8x40.step", [library.resolve(), second.resolve()]
    )

    assert root == library.resolve()


def _symlink(source: Path, destination: Path) -> None:
    """Create a directory symlink, skipping where the runner forbids one."""
    try:
        os.symlink(str(source), str(destination), target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip("this runner cannot create symlinks: %s" % error)
