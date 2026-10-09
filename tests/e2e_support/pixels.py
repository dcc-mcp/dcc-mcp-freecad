"""Bounded blue-bracket image witness; never changes pixels or imports GUI code."""

from collections import deque


def colored_geometry(data, width, height):
    assert type(width) is type(height) is int and 1 <= width <= 512 and 1 <= height <= 512
    total = width * height
    assert len(data) == total * 4, "Expected packed RGBA image data"
    mask = bytearray(total)
    for i in range(total):
        red, green, blue, alpha = data[i * 4 : i * 4 + 4]
        # Native diffuse blue may be shaded or highlighted. Gray/white background
        # and neutral overlays are excluded; exact display RGB is not assumed.
        mask[i] = (
            alpha >= 224
            and blue >= 48
            and blue - red >= 24
            and green - red >= 12
            and blue - green >= 10
        )
    count = sum(mask)
    assert 0.08 <= count / total <= 0.65, "No meaningful blue geometry coverage"
    largest, bounds = 0, None
    for start in range(total):
        if not mask[start]:
            continue
        mask[start] = 0
        queue = deque([start])
        size = 0
        low_x = high_x = start % width
        low_y = high_y = start // width
        while queue:
            index = queue.popleft()
            x, y = index % width, index // width
            size += 1
            low_x, high_x = min(low_x, x), max(high_x, x)
            low_y, high_y = min(low_y, y), max(high_y, y)
            adjacent = []
            if x:
                adjacent.append(index - 1)
            if x + 1 < width:
                adjacent.append(index + 1)
            if y:
                adjacent.append(index - width)
            if y + 1 < height:
                adjacent.append(index + width)
            for other in adjacent:
                if mask[other]:
                    mask[other] = 0
                    queue.append(other)
        if size > largest:
            largest, bounds = size, [low_x, low_y, high_x + 1, high_y + 1]
    assert largest / total >= 0.06, "Colored pixels are not a substantial connected region"
    span_x, span_y = bounds[2] - bounds[0], bounds[3] - bounds[1]
    assert span_x >= 0.25 * width and span_y >= 0.15 * height, (
        "Colored geometry region is too narrow"
    )
    assert largest / (span_x * span_y) >= 0.2, "Colored region is sparse"
    return {
        "profile": "blue-bracket-v1",
        "qualified": True,
        "colored_pixels": count,
        "coverage": count / total,
        "largest_component_pixels": largest,
        "largest_component_coverage": largest / total,
        "bounds_pixels": bounds,
        "image_pixels": total,
    }
