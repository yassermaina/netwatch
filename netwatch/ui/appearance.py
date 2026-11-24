"""Forced light/dark appearance that also beats user-level GTK colour overrides.

Desktop shells such as Noctalia write fixed libadwaita colours (``@window_bg_color`` and the ``:root`` custom
properties ``--window-bg-color``) into ``~/.config/gtk-4.0/*.css`` at user priority. ``Adw.StyleManager`` forcing
light/dark only flips the ``dark`` flag, so the Theme setting changed nothing visible. An explicit Light/Dark choice
therefore loads a provider one step above user priority; "Follow system" loads nothing, so the shell's colours (and
its live light/dark switch) apply untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

APPEARANCES = ("system", "light", "dark")
USER_PRIORITY_PLUS_ONE = 801  # Gtk.STYLE_PROVIDER_PRIORITY_USER is 800
PKG_DIR = Path(__file__).resolve().parent

# libadwaita default light colours (the dark scheme is NetWatch's own navy palette from style-dark.css).
LIGHT = {
    "accent_color": "#1c71d8", "accent_bg_color": "#3584e4", "accent_fg_color": "#ffffff",
    "destructive_bg_color": "#e01b24", "destructive_fg_color": "#ffffff",
    "error_bg_color": "#e01b24", "error_fg_color": "#ffffff", "error_color": "#c01c28",
    "window_bg_color": "#fafafb", "window_fg_color": "#323234",
    "view_bg_color": "#ffffff", "view_fg_color": "#323234",
    "headerbar_bg_color": "#ffffff", "headerbar_fg_color": "#323234", "headerbar_backdrop_color": "#fafafb",
    "popover_bg_color": "#ffffff", "popover_fg_color": "#323234",
    "card_bg_color": "#ffffff", "card_fg_color": "#323234",
    "dialog_bg_color": "#fafafb", "dialog_fg_color": "#323234",
    "sidebar_bg_color": "#ebebed", "sidebar_fg_color": "#323234", "sidebar_backdrop_color": "#f2f2f4",
    "sidebar_border_color": "#d4d4d8",
    "warning_bg_color": "#e5a50a", "warning_fg_color": "rgba(0, 0, 0, 0.8)", "warning_color": "#9c6e03",
    "success_bg_color": "#2ec27e", "success_fg_color": "#ffffff", "success_color": "#1b8553",
    "shade_color": "rgba(0, 0, 0, 0.07)",
}
# Fallback for the dark scheme completes whatever style-dark.css leaves out.
_DARK_BASE = {
    "accent_fg_color": "#ffffff", "destructive_bg_color": "#c01c28", "destructive_fg_color": "#ffffff",
    "error_bg_color": "#c01c28", "error_fg_color": "#ffffff", "error_color": "#ff7b63",
    "view_fg_color": "#ffffff", "headerbar_fg_color": "#ffffff", "popover_fg_color": "#ffffff",
    "card_fg_color": "#ffffff", "dialog_fg_color": "#ffffff", "sidebar_fg_color": "#ffffff",
    "sidebar_border_color": "#15171f",
    "warning_bg_color": "#cd9309", "warning_fg_color": "rgba(0, 0, 0, 0.8)", "warning_color": "#f8e45c",
    "success_bg_color": "#26a269", "success_fg_color": "#ffffff", "success_color": "#8ff0a4",
    "shade_color": "rgba(0, 0, 0, 0.36)",
}


def _dark_colors() -> dict:
    colors = dict(_DARK_BASE)
    text = (PKG_DIR / "style-dark.css").read_text(encoding="utf-8")
    for name, value in re.findall(r"@define-color\s+([a-z_]+)\s+(#[0-9a-fA-F]{3,8})\s*;", text):
        if not name.startswith("nw_"):
            colors[name] = value
    return colors


def override_css(appearance: str) -> str:
    """CSS pinning the colours of an explicit choice; empty for 'system'."""
    if appearance not in APPEARANCES:
        raise ValueError(f"Unknown appearance: {appearance}")
    if appearance == "system":
        return ""
    colors = LIGHT if appearance == "light" else _dark_colors()
    legacy = "\n".join(f"@define-color {name} {value};" for name, value in colors.items())
    variables = "\n".join(f"    --{name.replace('_', '-')}: {value};" for name, value in colors.items())
    return f"{legacy}\n:root {{\n{variables}\n}}"
