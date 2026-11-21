"""Color conversions between the web app (hex) and Hue (CIE xy, mirek)."""

from __future__ import annotations

import math

WHITE_XY = (0.3127, 0.3290)


def _to_linear(c: float) -> float:
    return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92


def _to_gamma(c: float) -> float:
    c = max(0.0, c)
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_xy(value: str) -> tuple[float, float]:
    h = value.lstrip("#")
    r, g, b = (_to_linear(int(h[i : i + 2], 16) / 255) for i in (0, 2, 4))
    x_ = r * 0.4124 + g * 0.3576 + b * 0.1805
    y_ = r * 0.2126 + g * 0.7152 + b * 0.0722
    z_ = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = x_ + y_ + z_
    if total == 0:
        return WHITE_XY
    return (round(x_ / total, 4), round(y_ / total, 4))


def xy_to_hex(x: float, y: float) -> str:
    if y <= 0:
        return "#ffffff"
    big_y = 1.0
    big_x = x * big_y / y
    big_z = (1 - x - y) * big_y / y
    r = big_x * 3.2406 - big_y * 1.5372 - big_z * 0.4986
    g = -big_x * 0.9689 + big_y * 1.8758 + big_z * 0.0415
    b = big_x * 0.0557 - big_y * 0.2040 + big_z * 1.0570
    peak = max(r, g, b)
    if peak > 1:
        r, g, b = r / peak, g / peak, b / peak
    return "#" + "".join(f"{round(min(1.0, _to_gamma(c)) * 255):02x}" for c in (r, g, b))


def mirek_to_hex(mirek: int) -> str:
    """Approximate display color for a white color temperature."""
    temp = 1_000_000 / max(mirek, 1) / 100
    if temp <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(temp) - 161.1195681661
        b = 0.0 if temp <= 19 else 138.5177312231 * math.log(temp - 10) - 305.0447927307
    else:
        r = 329.698727446 * (temp - 60) ** -0.1332047592
        g = 288.1221695283 * (temp - 60) ** -0.0755148492
        b = 255.0
    return "#" + "".join(f"{round(max(0.0, min(255.0, c))):02x}" for c in (r, g, b))
