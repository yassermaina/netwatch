"""Dialog helpers that work across Libadwaita 1.4+ (AlertDialog/Dialog when present)."""

from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Adw, Gtk

HAS_ADW_DIALOG = hasattr(Adw, "AlertDialog") and hasattr(Adw, "Dialog")


def choose(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    responses: list[tuple[str, str, str]],
    callback: Callable[[str], None],
    default: Optional[str] = None,
    cancel: str = "cancel",
) -> None:
    """responses: [(id, label, appearance)] where appearance is '', 'suggested' or 'destructive'."""
    appearances = {
        "suggested": Adw.ResponseAppearance.SUGGESTED,
        "destructive": Adw.ResponseAppearance.DESTRUCTIVE,
        "": Adw.ResponseAppearance.DEFAULT,
    }
    if HAS_ADW_DIALOG:
        dialog = Adw.AlertDialog(heading=heading, body=body)
    else:
        root = parent.get_root() if parent else None
        dialog = Adw.MessageDialog(transient_for=root, modal=True, heading=heading, body=body)
    for rid, text, appearance in responses:
        dialog.add_response(rid, text)
        dialog.set_response_appearance(rid, appearances[appearance])
    if default:
        dialog.set_default_response(default)
    dialog.set_close_response(cancel)
    dialog.connect("response", lambda _d, rid: callback(rid))
    if HAS_ADW_DIALOG:
        dialog.present(parent)
    else:
        dialog.present()


def confirm(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    confirm_label: str,
    on_confirm: Callable[[], None],
    destructive: bool = True,
) -> None:
    choose(
        parent,
        heading,
        body,
        [("cancel", "Cancel", ""), ("confirm", confirm_label, "destructive" if destructive else "suggested")],
        lambda rid: on_confirm() if rid == "confirm" else None,
        default="cancel" if destructive else "confirm",
    )


def message(parent: Gtk.Widget, heading: str, body: str) -> None:
    choose(parent, heading, body, [("ok", "OK", "suggested")], lambda _r: None, default="ok", cancel="ok")


def present_content(parent: Gtk.Widget, title: str, content: Gtk.Widget, width: int = 640, height: int = 640):
    """Show ``content`` in a modal window/dialog with a header bar.  Returns a close() callable."""
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_title_widget(Adw.WindowTitle(title=title))
    toolbar.add_top_bar(header)
    toolbar.set_content(content)
    if HAS_ADW_DIALOG:
        dialog = Adw.Dialog(title=title, content_width=width, content_height=height)
        dialog.set_child(toolbar)
        dialog.present(parent)
        return dialog.close
    win = Adw.Window(transient_for=parent.get_root(), modal=True, title=title, default_width=width,
                     default_height=height)
    win.set_content(toolbar)
    esc = Gtk.ShortcutController()
    esc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                      Gtk.CallbackAction.new(lambda *_: win.close() or True)))
    win.add_controller(esc)
    win.present()
    return win.close
