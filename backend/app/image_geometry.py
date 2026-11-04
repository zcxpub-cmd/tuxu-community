"""Validate orientation while recording a native tool's real, unmodified canvas."""
from __future__ import annotations

import re

_NAMED = {'square': 1.0, 'landscape': 1.5, 'portrait': 2 / 3}


def requested_dimensions(size):
    if not isinstance(size, str):
        return None
    match = re.fullmatch(r'([1-9]\d{0,4})x([1-9]\d{0,4})', size)
    return tuple(map(int, match.groups())) if match else None


def fits_requested_canvas(size, width, height, *, native=False):
    if any(type(value) is not int or value <= 0 for value in (width, height)):
        return False
    dimensions = requested_dimensions(size)
    expected = dimensions[0] / dimensions[1] if dimensions else _NAMED.get(size) if isinstance(size, str) else None
    if expected is None:
        return False
    actual = width / height
    if abs(actual / expected - 1) <= .05:
        return True
    # The built-in Codex tool has no enforced pixel-size parameter. Its ordinary
    # 4:3 / 3:2 / 16:9 / 2:1 canvases fulfil the UI's horizontal/vertical choice.
    # Do not extend this exception to arbitrary runners, opposite orientation,
    # squares, extreme panoramas, or nonstandard explicit requested ratios.
    if not native:
        return False
    if abs(expected - 1.5) < 1e-9:
        return any(abs(actual / ratio - 1) <= .05 for ratio in (4 / 3, 1.5, 16 / 9, 2.0))
    if abs(expected - 2 / 3) < 1e-9:
        return any(abs(actual / ratio - 1) <= .05 for ratio in (3 / 4, 2 / 3, 9 / 16, .5))
    return False


def native_canvas_notice(size, width, height):
    dimensions = requested_dimensions(size)
    if dimensions is None or dimensions == (width, height):
        return None
    return (f'原生模型实际返回 {width} × {height}，与请求的 {dimensions[0]} × {dimensions[1]} 不同。'
            '完整原图已保留，请按实际构图选用。')
