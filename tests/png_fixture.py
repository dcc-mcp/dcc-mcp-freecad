"""A minimal PNG encoder used only to build byte-exact test fixtures.

The adapter decodes PNGs with the standard library, so the tests that exercise
that decoder need real PNG bytes. Building them here keeps every fixture exact
and keeps a decoder bug from hiding behind a checked-in binary.
"""

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def chunk(kind, body):
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def paeth(left, upper, upper_left):
    predictor = left + upper - upper_left
    near_left = abs(predictor - left)
    near_upper = abs(predictor - upper)
    near_upper_left = abs(predictor - upper_left)
    if near_left <= near_upper and near_left <= near_upper_left:
        return left
    if near_upper <= near_upper_left:
        return upper
    return upper_left


def encode_row(raw, previous, channels, filter_type):
    out = bytearray(len(raw))
    for index in range(len(raw)):
        left = raw[index - channels] if index >= channels else 0
        above = previous[index]
        upper_left = previous[index - channels] if index >= channels else 0
        value = raw[index]
        if filter_type == 1:
            out[index] = (value - left) & 0xFF
        elif filter_type == 2:
            out[index] = (value - above) & 0xFF
        elif filter_type == 3:
            out[index] = (value - ((left + above) >> 1)) & 0xFF
        elif filter_type == 4:
            out[index] = (value - paeth(left, above, upper_left)) & 0xFF
        else:
            out[index] = value
    return bytes(out)


def png(width, height, rows, color_type=2, filter_type=0, interlace=0, bit_depth=8):
    """Encode unfiltered pixel rows as a PNG.

    ``rows`` is one bytes object per scanline, already in the target colour
    type's channel order and with no filter byte.
    """
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    channels = _CHANNELS[color_type]
    body = bytearray()
    previous = bytes(width * channels)
    for row in rows:
        body.append(filter_type)
        body += encode_row(row, previous, channels, filter_type)
        previous = row
    return SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(body)))


def solid(width, height, red, green, blue):
    """A flat fill: what a capture pipeline that never reached the scene writes."""
    row = bytes([red, green, blue]) * width
    return png(width, height, [row] * height)


def ramp(width, height):
    """A horizontal luminance gradient: varied, but containing no geometry.

    This is the fixture that matters most. FreeCAD's default 3D-view background
    is a linear gradient baked into the saved PNG, so an empty render looks like
    this, and any check based on variance alone accepts it.
    """
    rows = []
    for _ in range(height):
        row = bytearray()
        for column in range(width):
            value = int(255 * column / max(width - 1, 1))
            row += bytes([value, value, value])
        rows.append(bytes(row))
    return png(width, height, rows)


def checker(width, height, first=(16, 32, 48), second=(240, 224, 208)):
    rows = []
    for row_index in range(height):
        row = bytearray()
        for column in range(width):
            row += bytes(first if (row_index + column) % 2 == 0 else second)
        rows.append(bytes(row))
    return png(width, height, rows)
