"""The stylesheet's colour contract (W-30..W-32).

A theme change is the one visual edit that silently breaks readability: a green that
reads clearly on near-black is illegible on white, and nothing fails when it happens.
These tests parse the tokens out of the real stylesheet and check them, so switching
palettes cannot quietly ship text nobody can read.

WCAG AA is 4.5:1 for body text and 3:1 for large or incidental text. Both floors are
asserted against the surface each token is actually used on.
"""
import pathlib
import re

import pytest

CSS = (pathlib.Path(__file__).resolve().parents[2]
       / "src/schemavcs/web/static/app.css").read_text()


def tokens() -> dict[str, str]:
    """The `:root` custom properties whose values are plain hex colours."""
    root = CSS.split(":root {", 1)[1].split("\n}", 1)[0]
    return {m.group(1): m.group(2)
            for m in re.finditer(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", root)}


def _channel(c: int) -> float:
    x = c / 255
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# (foreground token, background token, floor) -- the floor is 4.5 where the token
# carries sentences and 3.0 where it carries a label, a pill or a placeholder.
BODY_TEXT = [
    ("ink", "bg", 4.5),
    ("ink", "surface", 4.5),
    ("ink-dim", "surface", 4.5),
    ("muted", "bg", 4.5),
    ("muted", "surface", 4.5),
    ("muted", "surface-2", 4.5),
    ("accent", "surface", 4.5),          # links
    ("accent", "bg", 4.5),
    ("accent-ink", "accent", 4.5),       # button label on its fill
    ("code-ink", "code-bg", 4.5),
    ("add", "surface", 4.5),             # diff tags and status chips
    ("drop", "surface", 4.5),
    ("rename", "surface", 4.5),
    ("alter", "surface", 4.5),
]


@pytest.mark.parametrize("fg,bg,floor", BODY_TEXT,
                         ids=[f"{f}-on-{b}" for f, b, _ in BODY_TEXT])
def test_W30_every_text_token_meets_its_contrast_floor(fg, bg, floor):
    t = tokens()
    got = contrast(t[fg], t[bg])

    assert got >= floor, (
        f"--{fg} ({t[fg]}) on --{bg} ({t[bg]}) is {got:.2f}:1, below {floor}:1")


def test_W31_colour_is_never_the_only_carrier_of_meaning():
    """Replaces a test whose premise was wrong.

    The first version asserted a luminance gap between `--add` and `--drop`, on the
    theory that a red and a green too close in brightness are confusable. But contrast
    ratio measures lightness, not hue, and forcing green and red apart in lightness
    would distort the palette to satisfy a metric that was never the real requirement.

    The real requirement is that colour is redundant: every coloured thing also carries
    a word. That is what makes the diff readable in greyscale, to a colour-blind reader,
    and in a terminal. So the property to pin is that the vocabulary is exhaustive --
    a change kind or safety level with no word would fall back to colour alone.
    """
    from schemavcs.engine.diff import ChangeKind
    from schemavcs.engine.plan import Safety
    from schemavcs.web.views import CHANGE_WORDS, SAFETY_WORDS

    assert set(CHANGE_WORDS) == set(ChangeKind), {
        "kinds with no wording": sorted(k.value for k in set(ChangeKind) - set(CHANGE_WORDS)),
    }
    assert set(SAFETY_WORDS) == set(Safety)
    assert all(word and word != kind.value for kind, (word, _) in CHANGE_WORDS.items())


def test_W32_placeholder_text_stays_above_the_incidental_floor():
    """Not a token, so it is easy to miss when the palette changes."""
    m = re.search(r"input::placeholder \{ color: (#[0-9a-fA-F]{6}); \}", CSS)
    assert m, "placeholder colour not found -- did the rule move?"

    got = contrast(m.group(1), tokens()["surface"])
    assert got >= 3.0, f"placeholder {m.group(1)} is {got:.2f}:1 on white"
