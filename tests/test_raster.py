"""Pixel-statistics tests for the headless render verifier.

These run without FreeCAD: the decision to refuse a waste image is the whole
point of `render_view`, so it is proven against synthesised frames here rather
than discovered on real hardware. Fixtures come from `png_fixture`, so every
file under test is a byte-exact PNG and a decoder bug cannot hide behind a
hand-written one.
"""

import struct
import zlib

import pytest
from png_fixture import SIGNATURE as _SIGNATURE
from png_fixture import checker as _checker
from png_fixture import chunk
from png_fixture import png as _png
from png_fixture import ramp as _ramp
from png_fixture import solid as _solid

from dcc_mcp_freecad import raster


def _summary(data, **kwargs):
    width, height, opaque, rgb = raster.decode_rgb(data, **kwargs)
    return raster.describe(width, height, opaque, raster.statistics(rgb))


# --- Decoding ---------------------------------------------------------------


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_every_png_filter_round_trips(filter_type):
    rows = []
    for row_index in range(5):
        row = bytearray()
        for column in range(7):
            row += bytes([(row_index * 37 + column * 11) % 256, column * 9 % 256, 200])
        rows.append(bytes(row))
    width, height, opaque, rgb = raster.decode_rgb(_png(7, 5, rows, filter_type=filter_type))
    assert (width, height, opaque) == (7, 5, 1.0)
    assert bytes(rgb) == b"".join(rows)


@pytest.mark.parametrize("color_type,channels", [(0, 1), (4, 2), (6, 4)])
def test_non_rgb_color_types_flatten_to_rgb(color_type, channels):
    width, height = 4, 2
    rows = [bytes(range(1, width * channels + 1))] * height
    _w, _h, opaque, rgb = raster.decode_rgb(_png(width, height, rows, color_type=color_type))
    assert len(rgb) == width * height * 3
    if channels == 4:
        # Alpha 4 over black, rounded.
        assert bytes(rgb[:3]) == bytes(
            [(1 * 4 + 127) // 255, (2 * 4 + 127) // 255, (3 * 4 + 127) // 255]
        )
        assert opaque == 0.0
    elif channels == 2:
        assert bytes(rgb[:3]) == bytes([(1 * 2 + 127) // 255] * 3)
        assert opaque == 0.0
    else:
        assert bytes(rgb[:3]) == bytes([1, 1, 1])
        assert opaque == 1.0


def test_opaque_rgba_frame_reports_full_opacity():
    rows = [bytes([10, 20, 30, 255]) * 3] * 3
    _w, _h, opaque, rgb = raster.decode_rgb(_png(3, 3, rows, color_type=6))
    assert opaque == 1.0
    assert bytes(rgb[:3]) == bytes([10, 20, 30])


# --- Refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "data,message",
    [
        (b"not an image at all", "not a PNG"),
        (_SIGNATURE + b"\x00\x00\x00\x01", "chunk header"),
        (_SIGNATURE, "no header chunk"),
    ],
)
def test_malformed_files_are_refused(data, message):
    with pytest.raises(raster.RasterError, match=message):
        raster.decode_rgb(data)


def test_a_corrupt_checksum_is_refused():
    good = bytearray(_solid(4, 4, 1, 2, 3))
    good[-1] ^= 0xFF
    with pytest.raises(raster.RasterError, match="checksum"):
        raster.decode_rgb(bytes(good))


def test_paletted_and_interlaced_captures_are_refused():
    with pytest.raises(raster.RasterError, match="colour type 3"):
        raster.decode_rgb(_png(2, 2, [b"\x00\x01"] * 2, color_type=3))
    with pytest.raises(raster.RasterError, match="interlaced"):
        raster.decode_rgb(_png(2, 2, [b"\x00\x01\x02"] * 2, interlace=1))


def test_truncated_image_data_is_refused():
    header = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    data = _SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" * 8))
    with pytest.raises(raster.RasterError, match="shorter than"):
        raster.decode_rgb(data)


def test_bounds_reject_an_oversized_capture():
    with pytest.raises(raster.RasterError, match="byte limit"):
        raster.decode_rgb(_solid(2, 2, 0, 0, 0), max_bytes=4)
    with pytest.raises(raster.RasterError, match="pixel limit"):
        raster.decode_rgb(_solid(8, 8, 0, 0, 0), max_pixels=4)


# --- Degeneracy -------------------------------------------------------------


@pytest.mark.parametrize("color", [(0, 0, 0), (255, 255, 255), (12, 34, 56)])
def test_a_flat_fill_is_called_degenerate(color):
    summary = _summary(_solid(32, 18, *color))
    reasons = raster.degeneracy_reasons(summary)
    assert reasons
    assert "single luminance level" in reasons[0]
    assert summary["luminance_stddev"] == 0.0
    assert summary["distinct_colors"] == 1
    assert summary["dominant_color_fraction"] == 1.0


def test_a_fully_transparent_capture_is_called_degenerate():
    rows = [bytes([10, 20, 30, 0]) * 8] * 8
    summary = _summary(_png(8, 8, rows, color_type=6))
    assert any("transparent" in reason for reason in raster.degeneracy_reasons(summary))


def test_a_frame_with_almost_one_color_is_degenerate():
    rows = []
    for row_index in range(64):
        row = bytearray([0, 0, 0] * 32)
        if row_index == 0:
            row[0:3] = b"\xff\xff\xff"
        rows.append(bytes(row))
    summary = _summary(_png(32, 64, rows))
    assert summary["dominant_color_fraction"] > raster.MAX_DOMINANT_COLOR_FRACTION
    assert raster.degeneracy_reasons(summary)


@pytest.mark.parametrize("factory", [_ramp, _checker])
def test_a_real_signal_is_not_called_degenerate(factory):
    summary = _summary(factory(64, 36))
    assert raster.degeneracy_reasons(summary) == []
    assert summary["pixel_count"] == 64 * 36
    assert summary["luminance_stddev"] > raster.MIN_LUMINANCE_STDDEV
    assert summary["luminance_levels"] >= raster.MIN_LUMINANCE_LEVELS
    assert summary["width"] == 64 and summary["height"] == 36
    assert summary["opaque_pixel_fraction"] == 1.0


def test_distinct_color_counting_is_capped_without_flipping_the_verdict():
    rows = []
    value = 0
    for _ in range(80):
        row = bytearray()
        for _ in range(80):
            row += bytes([value % 256, (value // 256) % 256, 128])
            value += 1
        rows.append(bytes(row))
    summary = _summary(_png(80, 80, rows))
    assert summary["distinct_colors_capped"] is True
    assert summary["distinct_colors"] == raster.MAX_TRACKED_COLORS
    assert raster.degeneracy_reasons(summary) == []


# --- Geometry contribution --------------------------------------------------


def test_a_gradient_background_alone_is_not_enough_to_pass_geometry():
    """The reason a second, empty-scene capture is required.

    FreeCAD's default 3D-view background is a linear gradient and it is baked
    into the saved PNG, so an empty render is varied. A variance-only check
    accepts it. Geometry is only proven against the same camera with nothing
    visible.
    """
    _w, _h, _opaque, rgb = raster.decode_rgb(_ramp(64, 36))
    summary = raster.describe(64, 36, 1.0, raster.statistics(rgb))
    assert raster.degeneracy_reasons(summary) == []
    assert summary["luminance_stddev"] > raster.MIN_LUMINANCE_STDDEV
    changed, fraction = raster.differing_pixel_fraction(rgb, bytearray(rgb))
    assert (changed, fraction) == (0, 0.0)
    assert raster.geometry_reasons(changed, 64 * 36, fraction)


def test_identical_captures_contribute_no_geometry():
    subject = bytearray([1, 2, 3] * 256)
    baseline = bytearray(subject)
    assert raster.differing_pixel_fraction(subject, baseline) == (0, 0.0)
    assert raster.geometry_reasons(0, 256, 0.0)


def test_a_changed_region_is_measured_exactly():
    total = 100 * 100
    subject = bytearray([0, 0, 0] * total)
    baseline = bytearray([0, 0, 0] * total)
    for row in range(20):
        for column in range(20):
            offset = 3 * (row * 100 + column)
            subject[offset : offset + 3] = b"\xff\xff\xff"
    changed, fraction = raster.differing_pixel_fraction(subject, baseline)
    assert changed == 400
    assert fraction == pytest.approx(0.04)
    assert raster.geometry_reasons(changed, total, fraction) == []


def test_a_sliver_below_the_minimum_is_refused():
    total = 100 * 100
    subject = bytearray([0, 0, 0] * total)
    baseline = bytearray([0, 0, 0] * total)
    for index in range(30):  # 0.03%, well under the 0.5% minimum
        subject[3 * index : 3 * index + 3] = b"\xff\xff\xff"
    changed, fraction = raster.differing_pixel_fraction(subject, baseline)
    assert changed == 30
    assert raster.geometry_reasons(changed, total, fraction)


def test_differences_below_the_channel_delta_are_ignored():
    subject = bytearray([10, 10, 10] * 64)
    baseline = bytearray([13, 13, 13] * 64)  # delta 3, under the threshold of 8
    assert raster.differing_pixel_fraction(subject, baseline) == (0, 0.0)


def test_mismatched_capture_sizes_cannot_be_compared():
    with pytest.raises(raster.RasterError, match="pixel counts"):
        raster.differing_pixel_fraction(bytearray(3 * 16), bytearray(3 * 25))
