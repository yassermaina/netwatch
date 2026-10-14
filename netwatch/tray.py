"""Optional status icon via the StatusNotifierItem D-Bus protocol.

GTK4 has no tray API and libappindicator is GTK3-only (it cannot be loaded
into a GTK4 process), so NetWatch exports org.kde.StatusNotifierItem and a
minimal com.canonical.dbusmenu itself using Gio.  This works with KDE,
XFCE/LXQt panels, and GNOME with the AppIndicator extension.  When no
StatusNotifierWatcher exists the tray simply reports itself unavailable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib

from netwatch import APP_ID, APP_NAME

log = logging.getLogger(__name__)

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
  </interface>
</node>
"""

ICONS = {
    "online": f"{APP_ID}-online-symbolic",
    "offline": f"{APP_ID}-offline-symbolic",
    "idle": f"{APP_ID}-idle-symbolic",
}


class MenuItem:
    def __init__(self, item_id: int, label: str = "", callback: Optional[Callable[[], None]] = None,
                 separator: bool = False, enabled: bool = True, toggle: Optional[bool] = None):
        self.id, self.label, self.callback = item_id, label, callback
        self.separator, self.enabled, self.toggle = separator, enabled, toggle

    def props(self) -> dict:
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        p = {"label": GLib.Variant("s", self.label), "enabled": GLib.Variant("b", self.enabled),
             "visible": GLib.Variant("b", True)}
        if self.toggle is not None:
            p["toggle-type"] = GLib.Variant("s", "checkmark")
            p["toggle-state"] = GLib.Variant("i", 1 if self.toggle else 0)
        return p


class StatusIcon:
    def __init__(self, on_activate: Callable[[], None], icon_theme_path: Optional[Path] = None):
        self.on_activate = on_activate
        self.icon_theme_path = str(icon_theme_path) if icon_theme_path and icon_theme_path.is_dir() else ""
        self.bus: Optional[Gio.DBusConnection] = None
        self.available = False
        self.error = ""
        self.icon = ICONS["idle"]
        self.tooltip = APP_NAME
        self.status = "Active"
        self.items: list[MenuItem] = []
        self.revision = 1
        self._reg_ids: list[int] = []
        self._name_id = 0
        self._watch_id = 0
        self.service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            self.error = f"no session bus: {exc.message}"
            return
        try:
            # In PyGObject, register_object() is the closure-based binding
            # (g_dbus_connection_register_object_with_closures).
            register = getattr(self.bus, "register_object_with_closures", None) or self.bus.register_object
            self._reg_ids.append(register(ITEM_PATH, self._sni_info, self._sni_call, self._sni_get, None))
            self._reg_ids.append(register(MENU_PATH, self._menu_info, self._menu_call, self._menu_get, None))
        except (GLib.Error, TypeError, AttributeError) as exc:
            self.error = f"cannot export D-Bus objects: {exc}"
            log.warning("tray unavailable: %s", self.error)
            return
        self._name_id = Gio.bus_own_name_on_connection(self.bus, self.service, Gio.BusNameOwnerFlags.NONE, None, None)
        self._watch_id = Gio.bus_watch_name_on_connection(
            self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_appeared, self._watcher_vanished)

    def stop(self) -> None:
        if self.bus is None:
            return
        for rid in self._reg_ids:
            self.bus.unregister_object(rid)
        self._reg_ids = []
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._name_id:
            Gio.bus_unown_name(self._name_id)
            self._name_id = 0
        self.available = False

    def status_text(self) -> str:
        if self.available:
            return "shown by a StatusNotifier host"
        if self.error:
            return self.error
        return ("no StatusNotifier host found (on GNOME install the AppIndicator extension); "
                "reopen NetWatch from the app launcher instead")

    def _watcher_appeared(self, conn, name, owner) -> None:
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.service,)), None, Gio.DBusCallFlags.NONE, 3000, None, self._registered)

    def _registered(self, conn, result) -> None:
        try:
            conn.call_finish(result)
            self.available = True
            self.error = ""
        except GLib.Error as exc:
            self.available = False
            self.error = f"registration failed: {exc.message}"

    def _watcher_vanished(self, conn, name) -> None:
        self.available = False

    # ------------------------------------------------------------ state
    def update(self, icon: str, tooltip: str, items: list[MenuItem]) -> None:
        icon_name = ICONS.get(icon, icon)
        if icon_name != self.icon:
            self.icon = icon_name
            self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon", None)
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
        self.items = items
        self.revision += 1
        self._signal(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))

    def _signal(self, path: str, iface: str, name: str, params) -> None:
        if self.bus is None or not self._reg_ids:
            return
        try:
            self.bus.emit_signal(None, path, iface, name, params)
        except GLib.Error as exc:
            log.debug("tray signal failed: %s", exc)

    # ------------------------------------------------------------ SNI
    def _sni_get(self, conn, sender, path, iface, prop):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", APP_NAME),
            "Status": GLib.Variant("s", self.status),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon),
            "IconThemePath": GLib.Variant("s", self.icon_theme_path),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], APP_NAME, self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), GLib.SOURCE_REMOVE)[1])
        invocation.return_value(None)

    # ------------------------------------------------------------ dbusmenu
    def _layout(self) -> GLib.Variant:
        children = [GLib.Variant("(ia{sv}av)", (i.id, i.props(), [])) for i in self.items]
        return GLib.Variant("(ia{sv}av)", (0, {"children-display": GLib.Variant("s", "submenu")}, children))

    def _menu_get(self, conn, sender, path, iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_theme_path] if self.icon_theme_path else []),
        }.get(prop)

    def _find(self, item_id: int) -> Optional[MenuItem]:
        return next((i for i in self.items if i.id == item_id), None)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        if method == "GetLayout":
            body = GLib.Variant.new_tuple(GLib.Variant("u", self.revision), self._layout())
            invocation.return_value(body)
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            items = [i for i in self.items if not ids or i.id in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i.id, i.props()) for i in items],)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            item = self._find(item_id)
            value = (item.props().get(name) if item else None) or GLib.Variant("s", "")
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event_id, _data, _ts = params.unpack()
            self._dispatch(item_id, event_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _data, _ts in params.unpack()[0]:
                self._dispatch(item_id, event_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _dispatch(self, item_id: int, event_id: str) -> None:
        item = self._find(item_id)
        if item and item.callback and event_id == "clicked":
            GLib.idle_add(lambda: (item.callback(), GLib.SOURCE_REMOVE)[1])
