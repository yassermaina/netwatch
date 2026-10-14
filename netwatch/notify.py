"""Desktop notifications through GApplication (portal or freedesktop service)."""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Gio

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, app: Gio.Application, setting: Callable[[str], object], window_visible: Callable[[], bool]):
        self.app = app
        self.setting = setting
        self.window_visible = window_visible
        self.sent: list[tuple[str, str]] = []

    def _allowed(self, kind: str) -> bool:
        mode = self.setting("notifications_mode")
        if mode == "never":
            return False
        if mode == "hidden" and self.window_visible():
            return False
        return bool(
            self.setting(
                {"outage": "notify_outage", "recovery": "notify_recovery", "error": "notify_errors"}.get(
                    kind, "notify_errors"
                )
            )
        )

    def notify(self, kind: str, title: str, body: str, force: bool = False) -> None:
        if not force and not self._allowed(kind):
            return
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_default_action("app.show-window")
        if kind == "outage":
            n.set_priority(Gio.NotificationPriority.HIGH)
            n.add_button("Open History", "app.show-history")
        try:
            n.set_icon(
                Gio.ThemedIcon.new(
                    {"outage": "network-error-symbolic", "recovery": "network-transmit-receive-symbolic"}.get(
                        kind, "dialog-warning-symbolic"
                    )
                )
            )
        except Exception:  # icon support is optional
            pass
        # one notification id per kind replaces older ones of the same kind
        try:
            self.app.send_notification(f"netwatch-{kind}", n)
            self.sent.append((kind, title))
        except Exception as exc:
            log.warning("notification failed: %s", exc)
