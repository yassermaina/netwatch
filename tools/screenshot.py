#!/usr/bin/env python3
"""Developer tool: render every page of the running app to PNG files (works under Xvfb).

    GSK_RENDERER=cairo xvfb-run -a python3 tools/screenshot.py --db /tmp/demo.db --out /tmp/shots
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from netwatch.main import setup_logging  # noqa: E402
from netwatch.ui.application import NetWatchApplication  # noqa: E402


def save_png(widget: Gtk.Widget, path: Path) -> None:
    w, h = widget.get_width(), widget.get_height()
    paintable = Gtk.WidgetPaintable.new(widget)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    renderer = widget.get_native().get_renderer()
    texture = renderer.render_texture(node, None)
    texture.save_to_png(str(path))
    print(f"saved {path} ({w}x{h})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--theme", default="dark", choices=["dark", "light"])
    ap.add_argument("--pages", default="history,overview,performance,reports,settings,about")
    ap.add_argument("--width", type=int, default=1240)
    ap.add_argument("--height", type=int, default=1500)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(False)
    app = NetWatchApplication(Path(args.db))
    pages = args.pages.split(",")

    def begin() -> bool:
        win = app.window
        if win is None:
            return True
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if args.theme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
        win.set_default_size(args.width, args.height)
        win.present()
        step(0)
        return False

    def step(i: int) -> None:
        if i >= len(pages):
            GLib.timeout_add(200, lambda: app.quit())
            return
        app.window.navigate(pages[i])

        def shoot() -> bool:
            save_png(app.window, out / f"{i + 1:02d}-{pages[i]}-{args.theme}.png")
            step(i + 1)
            return False

        GLib.timeout_add(2500, shoot)

    GLib.timeout_add(500, begin)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    os.environ.setdefault("GSK_RENDERER", "cairo")
    raise SystemExit(main())
