"""Main window: sidebar navigation (Adw.NavigationSplitView) and page stack."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from netwatch import APP_NAME
from netwatch.stats import Segment
from netwatch.ui import dialogs
from netwatch.ui.export import start_export
from netwatch.ui.pages.about import AboutPage
from netwatch.ui.pages.history import HistoryPage
from netwatch.ui.pages.overview import OverviewPage
from netwatch.ui.pages.performance import PerformancePage
from netwatch.ui.pages.reports import ReportsPage
from netwatch.ui.pages.settings import SettingsPage
from netwatch.ui.widgets.common import label

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication

PAGE_ORDER = ["overview", "performance", "history", "reports", "settings", "about"]


def _is_text_input(widget: Optional[Gtk.Widget]) -> bool:
    while widget is not None:
        if isinstance(widget, (Gtk.Editable, Gtk.Text, Gtk.TextView, Gtk.SpinButton)):
            return True
        widget = widget.get_parent()
    return False


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "NetWatchApplication"):
        super().__init__(application=app, title=APP_NAME, default_width=1120, default_height=860)
        self.app = app
        self.set_size_request(360, 480)
        self.pages = {
            "overview": OverviewPage(app),
            "performance": PerformancePage(app),
            "history": HistoryPage(app),
            "reports": ReportsPage(app),
            "settings": SettingsPage(app),
            "about": AboutPage(app),
        }
        self.current = "overview"
        self.nav_history: list[str] = []

        # sidebar
        self.sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar_list.add_css_class("navigation-sidebar")
        self.sidebar_rows: dict[str, Gtk.ListBoxRow] = {}
        for i, key in enumerate(PAGE_ORDER):
            page = self.pages[key]
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6)
            box.append(Gtk.Image.new_from_icon_name(page.icon))
            box.append(label(page.title))
            row.set_child(box)
            row.page_key = key
            row.set_tooltip_text(f"{page.title} (Ctrl+{i + 1})")
            self.sidebar_list.append(row)
            self.sidebar_rows[key] = row
        self.sidebar_list.connect("row-activated", lambda _l, row: self.navigate(row.page_key))
        sidebar_tv = Adw.ToolbarView()
        sb_header = Adw.HeaderBar()
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Keyboard Shortcuts", "app.shortcuts")
        section.append("About NetWatch", "app.about")
        menu.append_section(None, section)
        quit_section = Gio.Menu()
        quit_section.append("Quit", "app.quit")
        menu.append_section(None, quit_section)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu",
                                  primary=True)
        sb_header.pack_end(menu_btn)
        sidebar_tv.add_top_bar(sb_header)
        sb_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sb_scroll.set_child(self.sidebar_list)
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.append(sb_scroll)
        self.sidebar_status = label("", ("dim-label", "caption"), wrap=True)
        self.sidebar_status.set_margin_start(14)
        self.sidebar_status.set_margin_end(14)
        self.sidebar_status.set_margin_bottom(12)
        sidebar_box.append(self.sidebar_status)
        sidebar_tv.set_content(sidebar_box)
        sidebar_page = Adw.NavigationPage(title=APP_NAME, child=sidebar_tv)

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        for key, page in self.pages.items():
            self.stack.add_named(page, key)
        self.content_tv = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Overview")
        self.header.set_title_widget(self.title_widget)
        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic", tooltip_text="Mute alerts (M)",
                                         action_name="app.mute")
        self.header.pack_end(self.mute_btn)
        self.content_tv.add_top_bar(self.header)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self._on_banner_button)
        self._banner_action: Optional[Callable[[], None]] = None
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(self.banner)
        self.stack.set_vexpand(True)
        content_box.append(self.stack)
        self.content_tv.set_content(content_box)
        self.content_page = Adw.NavigationPage(title="Overview", child=self.content_tv)

        self.split = Adw.NavigationSplitView(sidebar=sidebar_page, content=self.content_page,
                                             min_sidebar_width=200, max_sidebar_width=260)
        self.split.set_show_content(True)
        self.toasts = Adw.ToastOverlay(child=self.split)
        self.set_content(self.toasts)

        self._install_breakpoints()

        self._install_actions()
        self._install_shortcuts()
        self.connect("close-request", self._on_close_request)
        self.navigate("overview", record=False)

    def _install_breakpoints(self) -> None:
        flows = []
        stack = [self]
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.FlowBox) and hasattr(w, "responsive_spec"):
                flows.append(w)
            child = w.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        # the last matching breakpoint wins, so add from widest to narrowest
        for tier, width in (("large", 1100), ("medium", 860), ("small", 720), ("tiny", 460)):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {width}sp"))
            if width <= 720:
                bp.add_setter(self.split, "collapsed", True)
            for fb in flows:
                n = fb.responsive_spec[tier]
                bp.add_setter(fb, "min-children-per-line", n)
                bp.add_setter(fb, "max-children-per-line", n)
            self.add_breakpoint(bp)

    # ------------------------------------------------------------ navigation
    def navigate(self, key: str, record: bool = True) -> None:
        if key not in self.pages:
            return
        if record and key != self.current:
            self.nav_history.append(self.current)
            self.nav_history = self.nav_history[-20:]
        old = self.pages.get(self.current)
        if old is not None and hasattr(old, "set_page_visible") and old is not self.pages[key]:
            old.set_page_visible(False)
        self.current = key
        page = self.pages[key]
        self.stack.set_visible_child_name(key)
        self.title_widget.set_title(page.title)
        self.content_page.set_title(page.title)
        self.sidebar_list.select_row(self.sidebar_rows[key])
        self.split.set_show_content(True)
        page.set_page_visible(True)

    def go_back(self) -> bool:
        if self.split.get_collapsed() and not self.split.get_show_content():
            return False
        if self.nav_history:
            self.navigate(self.nav_history.pop(), record=False)
            return True
        if self.split.get_collapsed():
            self.split.set_show_content(False)
            return True
        return False

    def show_history_segment(self, seg: Segment) -> None:
        self.navigate("history")
        GLib.timeout_add(300, lambda: (self.pages["history"]._on_timeline_activate(seg), GLib.SOURCE_REMOVE)[1])

    # ------------------------------------------------------------ actions
    def _install_actions(self) -> None:
        def add(name: str, cb: Callable[[], None]) -> None:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_: cb())
            self.add_action(act)

        add("open-history", lambda: self.navigate("history"))
        add("open-reports", lambda: self.navigate("reports"))
        add("preferences", lambda: self.navigate("settings"))
        add("export", self._export_shortcut)
        add("back", self.go_back)
        for i, key in enumerate(PAGE_ORDER):
            add(f"page-{i + 1}", lambda k=key: self.navigate(k))

    def _install_shortcuts(self) -> None:
        ctrl = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.BUBBLE)
        ctrl.set_scope(Gtk.ShortcutScope.LOCAL)

        def guarded(callback: Callable[[], None]):
            def run(widget, args) -> bool:
                if _is_text_input(self.get_focus()):
                    return False  # never steal keys from text entry
                callback()
                return True
            return Gtk.CallbackAction.new(run)

        def act(name: str):
            return guarded(lambda: self.app.activate_action(name, None))

        ctrl.add_shortcut(Gtk.Shortcut.new(Gtk.KeyvalTrigger.new(Gdk.KEY_m, 0), act("mute")))
        ctrl.add_shortcut(Gtk.Shortcut.new(Gtk.KeyvalTrigger.new(Gdk.KEY_space, 0), act("toggle-pause")))
        ctrl.add_shortcut(Gtk.Shortcut.new(Gtk.KeyvalTrigger.new(Gdk.KEY_Escape, 0),
                                           Gtk.CallbackAction.new(lambda *_: self.go_back())))
        self.add_controller(ctrl)

    def _export_shortcut(self) -> None:
        if self.current == "history":
            self.pages["history"].export()
        elif self.current == "reports":
            self.pages["reports"].export()
        else:
            self.start_export(period="7d")

    def start_export(self, period: str = "all", custom=None, focus: Optional[Segment] = None,
                     fmt: Optional[str] = None) -> None:
        start_export(self.app, self, period, custom, focus, fmt)

    # ------------------------------------------------------------ feedback
    def toast(self, text: str, button: Optional[tuple[str, Callable[[], None]]] = None, timeout: int = 4) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        if button:
            t.set_button_label(button[0])
            t.connect("button-clicked", lambda *_: button[1]())
        self.toasts.add_toast(t)

    def show_banner(self, text: Optional[str], button: Optional[str] = None,
                    action: Optional[Callable[[], None]] = None) -> None:
        if not text:
            self.banner.set_revealed(False)
            return
        self.banner.set_title(text)
        self.banner.set_button_label(button)
        self._banner_action = action
        self.banner.set_revealed(True)

    def _on_banner_button(self, *_a) -> None:
        if self._banner_action:
            self._banner_action()
        self.banner.set_revealed(False)

    def set_muted_indicator(self, muted: bool) -> None:
        self.mute_btn.set_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")
        self.mute_btn.set_tooltip_text("Unmute alerts (M)" if muted else "Mute alerts (M)")
        self.pages["overview"].on_mute_changed(muted)

    # ------------------------------------------------------------ closing
    def _on_close_request(self, *_a) -> bool:
        settings_page = self.pages["settings"]
        if settings_page.has_unsaved():
            dialogs.choose(
                self, "Save changes to settings?", "Some settings have not been saved.",
                [("cancel", "Cancel", ""), ("discard", "Discard", "destructive"), ("save", "Save", "suggested")],
                lambda rid: self._resolve_unsaved(rid), default="save",
            )
            return True
        return self.app.on_window_close_request()

    def _resolve_unsaved(self, rid: str) -> None:
        page = self.pages["settings"]
        if rid == "cancel":
            self.navigate("settings")
            return
        if rid == "save" and not page.save():
            self.navigate("settings")
            return
        if rid == "discard":
            page.load()
        self.close()
