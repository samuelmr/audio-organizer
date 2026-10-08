"""The colours of the web UI stay readable whatever hue the theme is given.

The surfaces and the accent are tinted by --hue; --ok, --bad and --warn are fixed, as they
carry meaning. Every text colour must have a contrast of at least 4.5:1 (WCAG AA) on every
surface it can be shown on, in both colour schemes, for every hue, and be within sRGB so
that no hue gets clipped into a different colour.
"""

import math
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parent.parent / "src/audio_organizer/web/static/style.css").read_text()

TEXT = ["fg", "muted", "accent", "ok", "bad", "warn"]
# translucent surfaces are composited over --bg, as on the page
SURFACES = ["bg", "highlight", "accent-soft", "bg-glow"]
OKLCH = re.compile(
    r"--([\w-]+):\s*oklch\(\s*([\d.]+)\s+([\d.]+)\s+(var\(--hue\)|[\d.]+)\s*(?:/\s*([\d.]+))?\s*\)"
)


def _tokens(block: str) -> dict[str, tuple[float, float, float | None, float]]:
    return {
        name: (float(L), float(C), None if "var" in H else float(H), float(A or 1))
        for name, L, C, H, A in OKLCH.findall(block)
    }


def _schemes() -> dict[str, dict]:
    light = _tokens(CSS[: CSS.index("@media (prefers-color-scheme: dark)")])
    dark_block = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{(.*?)\n\}", CSS, re.S)
    return {"light": light, "dark": {**light, **_tokens(dark_block.group(1))}}


def _srgb(L: float, C: float, h: float) -> tuple[float, float, float]:
    """OKLCH to gamma-encoded sRGB, unclipped."""
    a, b = C * math.cos(math.radians(h)), C * math.sin(math.radians(h))
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(
        12.92 * c if c <= 0.0031308 else 1.055 * math.copysign(abs(c) ** (1 / 2.4), c) - 0.055
        for c in linear
    )


def _luminance(rgb) -> float:
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _color(tokens, name, hue):
    L, C, H, alpha = tokens[name]
    rgb = _srgb(L, C, hue if H is None else H)
    if alpha < 1:
        under = _color(tokens, "bg", hue)
        rgb = tuple(alpha * c + (1 - alpha) * u for c, u in zip(rgb, under, strict=True))
    return rgb


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_is_readable_on_every_surface_for_every_hue(scheme):
    tokens = _schemes()[scheme]
    failures = []
    for hue in range(0, 360, 5):
        for text in TEXT:
            for surface in SURFACES:
                ratio = _contrast(_color(tokens, text, hue), _color(tokens, surface, hue))
                if ratio < 4.5:
                    failures.append(f"{text} on {surface} at hue {hue}: {ratio:.2f}")
        # a primary button: the page background as text on the accent
        ratio = _contrast(_color(tokens, "bg", hue), _color(tokens, "accent", hue))
        if ratio < 4.5:
            failures.append(f"bg on accent at hue {hue}: {ratio:.2f}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_colours_are_within_srgb_for_every_hue(scheme):
    tokens = _schemes()[scheme]
    outside = [
        f"{name} at hue {hue}"
        for hue in range(0, 360, 5)
        for name, (L, C, H, _) in tokens.items()
        if any(not -0.002 <= c <= 1.002 for c in _srgb(L, C, hue if H is None else H))
    ]
    assert not outside, ", ".join(outside)
