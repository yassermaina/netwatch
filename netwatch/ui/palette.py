"""State colours for custom-drawn widgets (Cairo), matching style.css."""

from __future__ import annotations

from gi.repository import Adw

from netwatch.models import ConnState

DARK = {
    "online": (0.204, 0.780, 0.349),  # #34c759
    "minor": (0.627, 0.788, 0.231),  # yellow-green mixed day
    "no_internet": (1.0, 0.624, 0.039),  # #ff9f0a
    "major": (0.957, 0.471, 0.094),  # deeper orange
    "not_connected": (0.557, 0.557, 0.576),  # #8e8e93
    "unknown": (0.369, 0.361, 0.902),  # indigo
    "unmonitored": (0.86, 0.86, 0.88),
    "hatch": (0.62, 0.62, 0.66),
    "outside": (1, 1, 1),
    "fg": (0.95, 0.95, 0.97),
    "muted": (0.62, 0.63, 0.68),
    "grid": (1, 1, 1),
    "accent": (0.231, 0.486, 0.965),
    "error": (1.0, 0.27, 0.23),
}

LIGHT = dict(
    DARK,
    online=(0.149, 0.651, 0.267),
    minor=(0.545, 0.706, 0.137),
    no_internet=(0.941, 0.549, 0.0),
    major=(0.878, 0.384, 0.039),
    not_connected=(0.50, 0.50, 0.53),
    unmonitored=(0.80, 0.80, 0.83),
    hatch=(0.60, 0.60, 0.64),
    outside=(0, 0, 0),
    fg=(0.11, 0.11, 0.12),
    muted=(0.42, 0.42, 0.45),
    grid=(0, 0, 0),
    accent=(0.11, 0.42, 0.88),
)

STATE_KEYS = {
    ConnState.ONLINE: "online",
    ConnState.NO_INTERNET: "no_internet",
    ConnState.NOT_CONNECTED: "not_connected",
    ConnState.UNKNOWN: "unknown",
    ConnState.PAUSED: "unmonitored",
    ConnState.NOT_RUNNING: "unmonitored",
}

SERIES = [
    (0.231, 0.553, 0.965),
    (0.204, 0.780, 0.349),
    (1.0, 0.624, 0.039),
    (0.749, 0.353, 0.949),
    (0.0, 0.749, 0.792),
    (1.0, 0.271, 0.427),
]


def colors() -> dict:
    return DARK if Adw.StyleManager.get_default().get_dark() else LIGHT


def state_color(state: ConnState) -> tuple:
    return colors()[STATE_KEYS[state]]


def css_class(state: ConnState) -> str:
    return STATE_KEYS[state].replace("_", "-")


def hatch(cr, x: float, y: float, w: float, h: float, alpha: float = 1.0, spacing: float = 5.0) -> None:
    """Fill a rectangle with a light diagonal hatch (unmonitored time)."""
    c = colors()
    cr.save()
    cr.rectangle(x, y, w, h)
    cr.clip()
    base = c["unmonitored"]
    cr.set_source_rgba(*base, 0.22 * alpha)
    cr.paint()
    cr.set_source_rgba(*c["hatch"], 0.55 * alpha)
    cr.set_line_width(1.0)
    i = -h
    while i < w + h:
        cr.move_to(x + i, y + h)
        cr.line_to(x + i + h, y)
        i += spacing
    cr.stroke()
    cr.restore()


def rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.14159)
    cr.arc(x + r, y + r, r, 3.14159, 4.71239)
    cr.close_path()
