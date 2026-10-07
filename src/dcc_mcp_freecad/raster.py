"""Bounded PNG pixel statistics for headless render verification.

A render is only useful if the caller can tell a real picture from a broken
capture. Competitor implementations report success and hand back a frame that
is entirely black (NVIDIA/Wayland), entirely white (Windows, GPU content never
captured) or the previous frame (a capture pipeline that stalled after a camera
move). All three are silent: the tool says "ok" and the agent reasons about a
picture that contains nothing.

So the image bytes are judged here, in the adapter process, before anything is
returned. Two independent questions are answered and both must pass:

* **Is the frame a picture at all?** A flat frame means the capture pipeline
  never reached the scene. This is cheaper than it sounds -- a single
  luminance level, a near-zero spread, or one colour covering essentially every
  pixel is enough to refuse.
* **Did the selected objects contribute anything?** Pixel statistics alone
  cannot answer this. FreeCAD's default 3D-view background is a linear
  gradient (dark blue to blue-grey) and it is *baked into the saved PNG*, so an
  entirely empty render already has plenty of variance and passes any
  "is it monochrome?" test. Every render is therefore captured twice: once as
  requested, and once with every object hidden at the identical camera. The
  second frame is what the scene looks like with nothing selected, and a subject
  that does not differ from it rendered no geometry -- however colourful its
  background is.

The module is pure standard library. The wrapper runs on Python 3.7 and the
host side ships no image dependency this adapter can rely on, so the PNG is
decoded with ``zlib`` and ``struct`` and nothing else.
"""

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# --- Decision thresholds -------------------------------------------------
#
# Every threshold below is deliberately close to "obviously broken" rather than
# "looks good". This module refuses frames that carry no signal; it does not
# attempt to judge image quality. A frame that passes is not guaranteed to be
# attractive, only to be non-degenerate and to contain the requested geometry.

#: Minimum luminance spread, in 0-255 units, before a frame is called flat.
#: A solid fill is exactly 0. This only has to exceed encoding noise.
MIN_LUMINANCE_STDDEV = 1.0

#: Minimum number of occupied bins in a 256-bin luminance histogram. A frame
#: with one level is a fill colour, whatever that colour is.
MIN_LUMINANCE_LEVELS = 2

#: Above this share of one exact colour, the frame is a fill with perhaps a
#: handful of stray pixels, not a render.
MAX_DOMINANT_COLOR_FRACTION = 0.999

#: Minimum share of pixels that must differ from the empty-scene capture for
#: the selection to count as visible. 0.5% of 1280x720 is ~4600 pixels, so a
#: thin sliver still passes while anti-aliasing noise does not. The measured
#: fraction is always returned, so a caller can see how close it came.
MIN_GEOMETRY_PIXEL_FRACTION = 0.005

#: Per-channel difference, in 0-255 units, that marks a pixel as changed
#: between the subject and the empty-scene capture.
GEOMETRY_PIXEL_DELTA = 8

# --- Resource bounds -----------------------------------------------------

#: A render is bounded by the capture dimensions, so these are backstops
#: against a host that writes something unexpected, not the real limit.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 4 * 1024 * 1024

#: Distinct colours are counted only to detect a flat frame, so counting stops
#: here. A frame with more colours than this is by construction not flat.
MAX_TRACKED_COLORS = 4096

_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}

# Rec.709 luma as integers: (54 + 183 + 19) == 256, so one shift to scale.
_LUMA = (54, 183, 19)


class RasterError(ValueError):
    """A raster artefact this adapter refuses to judge or return."""


def _chunks(data):
    """Yield ``(kind, body)`` for every chunk, verifying each CRC.

    A corrupt or partially written file is refused rather than decoded: a
    truncated render is exactly the case where a silent partial decode would
    produce plausible-looking statistics.
    """
    if len(data) < len(PNG_SIGNATURE) or data[: len(PNG_SIGNATURE)] != PNG_SIGNATURE:
        raise RasterError("the captured file is not a PNG")
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        if offset + 8 > len(data):
            raise RasterError("the PNG ends inside a chunk header")
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body_start = offset + 8
        body_end = body_start + length
        if body_end + 4 > len(data):
            raise RasterError("PNG chunk %s is truncated" % kind.decode("ascii", "replace"))
        body = data[body_start:body_end]
        (stored_crc,) = struct.unpack(">I", data[body_end : body_end + 4])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != stored_crc:
            raise RasterError("PNG chunk %s failed its checksum" % kind.decode("ascii", "replace"))
        yield kind, body
        offset = body_end + 4


def _paeth(left, upper, upper_left):
    predictor = left + upper - upper_left
    distance_left = abs(predictor - left)
    distance_upper = abs(predictor - upper)
    distance_upper_left = abs(predictor - upper_left)
    if distance_left <= distance_upper and distance_left <= distance_upper_left:
        return left
    if distance_upper <= distance_upper_left:
        return upper
    return upper_left


def _unfilter(raw, height, stride, channels):
    """Undo the per-scanline PNG filters into a flat byte row sequence."""
    result = bytearray(height * stride)
    previous = bytes(stride)
    position = 0
    for row in range(height):
        if position + 1 > len(raw):
            raise RasterError("the PNG image data ended early")
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        if len(line) != stride:
            raise RasterError("PNG scanline %d is truncated" % row)
        position += stride
        if filter_type == 1:
            for index in range(channels, stride):
                line[index] = (line[index] + line[index - channels]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                above = previous[index]
                left = line[index - channels] if index >= channels else 0
                line[index] = (line[index] + ((left + above) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                above = previous[index]
                if index >= channels:
                    left = line[index - channels]
                    upper_left = previous[index - channels]
                else:
                    left = 0
                    upper_left = 0
                line[index] = (line[index] + _paeth(left, above, upper_left)) & 0xFF
        elif filter_type != 0:
            raise RasterError("PNG scanline %d uses unsupported filter %d" % (row, filter_type))
        result[row * stride : (row + 1) * stride] = line
        previous = line
    return result


def decode_rgb(data, max_bytes=MAX_IMAGE_BYTES, max_pixels=MAX_IMAGE_PIXELS):
    """Decode an 8-bit PNG into ``(width, height, opaque_fraction, rgb)``.

    Paletted and interlaced PNGs are refused rather than approximated: this
    adapter only ever asks for a truecolour capture, so a file in another form
    means the host wrote something this verifier cannot honestly judge.

    Non-opaque pixels are composited over black, because that is how an opaque
    render is seen. The opaque share is reported alongside so a fully
    transparent capture is visible as such instead of being judged as black.
    """
    if len(data) > max_bytes:
        raise RasterError(
            "the captured PNG is %d bytes, over the %d byte limit" % (len(data), max_bytes)
        )
    width = height = depth = color_type = interlace = None
    compressed = []
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            if len(body) < 13:
                raise RasterError("the PNG header is too short")
            width, height, depth, color_type, _compression, _filter, interlace = struct.unpack(
                ">IIBBBBB", body[:13]
            )
        elif kind == b"IDAT":
            compressed.append(body)
        elif kind == b"IEND":
            break
    if width is None:
        raise RasterError("the PNG has no header chunk")
    if depth != 8:
        raise RasterError("only 8-bit PNG captures are supported, got bit depth %d" % depth)
    if interlace != 0:
        raise RasterError("interlaced PNG captures are not supported")
    if color_type not in _CHANNELS:
        raise RasterError(
            "unsupported PNG colour type %d; only greyscale and truecolour are accepted"
            % color_type
        )
    if not compressed:
        raise RasterError("the PNG has no image data")
    if width == 0 or height == 0:
        raise RasterError("the PNG is empty")
    pixels = width * height
    if pixels > max_pixels:
        raise RasterError(
            "the captured PNG is %dx%d, over the %d pixel limit" % (width, height, max_pixels)
        )
    channels = _CHANNELS[color_type]
    try:
        raw = zlib.decompress(b"".join(compressed))
    except zlib.error as error:
        raise RasterError("the PNG image data could not be decompressed: %s" % error) from None
    stride = width * channels
    if len(raw) < height * (stride + 1):
        raise RasterError("the PNG image data is shorter than %d scanlines" % height)
    flat = _unfilter(raw, height, stride, channels)

    rgb = bytearray(pixels * 3)
    opaque = 0
    if channels == 3:
        rgb = flat
        opaque = pixels
    elif channels == 1:
        for index, value in enumerate(flat):
            rgb[3 * index] = rgb[3 * index + 1] = rgb[3 * index + 2] = value
        opaque = pixels
    elif channels == 2:  # grey + alpha
        for index in range(pixels):
            value = flat[2 * index]
            alpha = flat[2 * index + 1]
            if alpha == 255:
                opaque += 1
            scaled = (value * alpha + 127) // 255
            rgb[3 * index] = rgb[3 * index + 1] = rgb[3 * index + 2] = scaled
    else:  # RGB + alpha
        for index in range(pixels):
            alpha = flat[4 * index + 3]
            if alpha == 255:
                opaque += 1
            rgb[3 * index] = (flat[4 * index] * alpha + 127) // 255
            rgb[3 * index + 1] = (flat[4 * index + 1] * alpha + 127) // 255
            rgb[3 * index + 2] = (flat[4 * index + 2] * alpha + 127) // 255
    return width, height, opaque / float(pixels), rgb


def statistics(rgb):
    """Summarise flattened RGB bytes with bounded, decision-ready numbers."""
    total = len(rgb) // 3
    if total == 0:
        raise RasterError("the captured PNG contains no pixels")
    red_weight, green_weight, blue_weight = _LUMA
    histogram = [0] * 256
    colors = {}
    capped = False
    total_luma = 0
    total_squared = 0
    for red, green, blue in zip(rgb[0::3], rgb[1::3], rgb[2::3]):
        luma = (red * red_weight + green * green_weight + blue * blue_weight) >> 8
        histogram[luma] += 1
        total_luma += luma
        total_squared += luma * luma
        key = (red << 16) | (green << 8) | blue
        if key in colors:
            colors[key] += 1
        elif len(colors) < MAX_TRACKED_COLORS:
            colors[key] = 1
        else:
            capped = True
    mean = total_luma / float(total)
    variance = max(total_squared / float(total) - mean * mean, 0.0)
    dominant_key, dominant_count = max(colors.items(), key=lambda item: item[1])
    return {
        "pixel_count": total,
        "distinct_colors": len(colors),
        "distinct_colors_capped": capped,
        "dominant_color": [
            dominant_key >> 16 & 0xFF,
            dominant_key >> 8 & 0xFF,
            dominant_key & 0xFF,
        ],
        "dominant_color_fraction": dominant_count / float(total),
        "luminance_min": next(index for index, count in enumerate(histogram) if count),
        "luminance_max": next(index for index in range(255, -1, -1) if histogram[index]),
        "luminance_mean": mean,
        "luminance_stddev": variance**0.5,
        "luminance_levels": sum(1 for count in histogram if count),
    }


def describe(width, height, opaque_fraction, stats):
    """The full pixel summary a caller needs to trust or dispute a render."""
    summary = {
        "width": width,
        "height": height,
        "opaque_pixel_fraction": opaque_fraction,
    }
    summary.update(stats)
    return summary


def degeneracy_reasons(summary):
    """Why a frame carries no signal, or ``[]`` when it does carry some.

    This is the check the competitor implementations have no equivalent of: it
    turns "the capture was black/white/stale" from an invisible defect into a
    named, returned failure.
    """
    reasons = []
    if summary["luminance_levels"] < MIN_LUMINANCE_LEVELS:
        reasons.append(
            "the frame has a single luminance level (%d), so it is a flat fill rather "
            "than a render" % summary["luminance_levels"]
        )
    if summary["luminance_stddev"] < MIN_LUMINANCE_STDDEV:
        reasons.append(
            "the frame's luminance spread is %.4f, below the %.1f minimum for a real "
            "render" % (summary["luminance_stddev"], MIN_LUMINANCE_STDDEV)
        )
    if summary["dominant_color_fraction"] > MAX_DOMINANT_COLOR_FRACTION:
        reasons.append(
            "one colour covers %.5f of the frame, above the %.3f maximum"
            % (summary["dominant_color_fraction"], MAX_DOMINANT_COLOR_FRACTION)
        )
    if summary.get("opaque_pixel_fraction") is not None and summary["opaque_pixel_fraction"] <= 0:
        reasons.append("every pixel is transparent, so the capture contains no visible content")
    return reasons


def differing_pixel_fraction(subject, baseline, delta=GEOMETRY_PIXEL_DELTA):
    """Share of pixels where ``subject`` differs from ``baseline`` per channel.

    ``baseline`` is the same camera and the same view with every object hidden,
    so this measures exactly what the selection added to the frame -- the
    non-background share, counted without having to know what the background is.
    """
    total = len(subject) // 3
    if total != len(baseline) // 3:
        raise RasterError("the subject and baseline captures have different pixel counts")
    if total == 0:
        raise RasterError("the captures contain no pixels")
    if bytes(subject) == bytes(baseline):
        return 0, 0.0
    changed = 0
    for first_red, first_green, first_blue, second_red, second_green, second_blue in zip(
        subject[0::3], subject[1::3], subject[2::3], baseline[0::3], baseline[1::3], baseline[2::3]
    ):
        if (
            abs(first_red - second_red) >= delta
            or abs(first_green - second_green) >= delta
            or abs(first_blue - second_blue) >= delta
        ):
            changed += 1
    return changed, changed / float(total)


def geometry_reasons(changed_pixels, total_pixels, changed_fraction):
    """Why a frame shows no geometry, or ``[]`` when it does."""
    if changed_fraction < MIN_GEOMETRY_PIXEL_FRACTION:
        return [
            "the selection changed %d of %d pixels (%.5f), below the %.3f minimum, so the "
            "rendered frame does not differ from an empty scene at the same camera"
            % (changed_pixels, total_pixels, changed_fraction, MIN_GEOMETRY_PIXEL_FRACTION)
        ]
    return []
