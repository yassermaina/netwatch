"""User preferences: defaults, validation and a thread-safe store backed by SQLite."""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from netwatch.storage.db import Database

DEFAULTS: dict[str, Any] = {
    # Monitoring
    "interval_seconds": 5,
    "timeout_seconds": 2,
    "failure_threshold": 2,
    "recovery_threshold": 2,
    "latency_warning_ms": 150,
    "loss_warning_pct": 5,
    "start_monitoring_on_launch": True,
    # Alerts
    "sound_enabled": True,
    "sound_volume": 80,
    "outage_sound_style": "alarm",  # see alerts.OUTAGE_SOUNDS
    "outage_sound_file": "",
    "recovery_sound_style": "chime",  # see alerts.RECOVERY_SOUNDS
    "recovery_sound_file": "",
    "sound_repeat": True,
    "sound_repeat_seconds": 15,
    "sound_recovery": True,
    "resume_alert_on_unmute": True,
    "start_muted": False,
    "muted": False,
    # Notifications: always | hidden | never
    "notifications_mode": "always",
    "notify_outage": True,
    "notify_recovery": True,
    "notify_errors": True,
    # Appearance: system | light | dark ; comfortable | compact
    "theme": "system",
    "density": "comfortable",
    # Background
    "hide_on_close": True,
    "tray_enabled": True,
    "launch_at_login": False,
    "background_notice_shown": False,
    # Data
    "reports_dir": "",
    "retention_days": 90,
}

DEFAULT_TARGETS = [
    ("Cloudflare DNS", "1.1.1.1", "icmp"),
    ("Google DNS", "8.8.8.8", "icmp"),
    ("Quad9 DNS", "9.9.9.9", "icmp"),
]

CHECK_TYPES = {"icmp": "ICMP ping", "dns": "DNS lookup", "http": "HTTP(S) request"}

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$")


@dataclass
class ValidationError:
    key: str
    message: str


def _int_range(key: str, lo: int, hi: int, label: str) -> Callable[[dict], list[ValidationError]]:
    def check(values: dict) -> list[ValidationError]:
        v = values.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= v <= hi):
            return [ValidationError(key, f"{label} must be between {lo} and {hi}.")]
        return []

    return check


_RULES = [
    _int_range("interval_seconds", 1, 3600, "Check interval"),
    _int_range("timeout_seconds", 1, 60, "Timeout"),
    _int_range("failure_threshold", 1, 20, "Failure threshold"),
    _int_range("recovery_threshold", 1, 20, "Recovery threshold"),
    _int_range("latency_warning_ms", 1, 10000, "Latency warning threshold"),
    _int_range("loss_warning_pct", 1, 100, "Packet loss warning threshold"),
    _int_range("sound_volume", 0, 100, "Volume"),
    _int_range("sound_repeat_seconds", 3, 3600, "Alert repeat interval"),
    _int_range("retention_days", 0, 3650, "Measurement retention"),
]

_ENUMS = {
    "outage_sound_style": {"alarm", "siren", "beep", "heartbeat", "custom"},
    "recovery_sound_style": {"chime", "beep", "custom"},
    "notifications_mode": {"always", "hidden", "never"},
    "theme": {"system", "light", "dark"},
    "density": {"comfortable", "compact"},
}


def validate(values: dict) -> list[ValidationError]:
    """Validate a complete settings dict, including cross-field rules."""
    errors: list[ValidationError] = []
    for rule in _RULES:
        errors.extend(rule(values))
    for key, allowed in _ENUMS.items():
        if values.get(key) not in allowed:
            errors.append(ValidationError(key, f"Invalid value for {key}."))
    for kind, label in (("outage", "outage"), ("recovery", "recovery")):
        if values.get(f"{kind}_sound_style") == "custom":
            from netwatch.alerts import check_sound_file  # noqa: PLC0415

            problem = check_sound_file(str(values.get(f"{kind}_sound_file") or ""), None)
            if problem:
                errors.append(ValidationError(f"{kind}_sound_file", f"Custom {label} sound: {problem}"))
    interval, timeout = values.get("interval_seconds"), values.get("timeout_seconds")
    if (
        isinstance(interval, (int, float))
        and isinstance(timeout, (int, float))
        and timeout >= interval
        and interval > 1
    ):
        errors.append(
            ValidationError(
                "timeout_seconds",
                f"Timeout ({timeout}s) must be shorter than the check interval ({interval}s), "
                "otherwise checks would overlap.",
            )
        )
    return errors


def validate_target(name: str, address: str, check_type: str) -> list[str]:
    problems = []
    address = address.strip()
    if not name.strip():
        problems.append("Give the target a name.")
    if check_type not in CHECK_TYPES:
        problems.append("Unknown check type.")
    if not address:
        problems.append("Enter an address.")
    elif check_type == "http":
        if not re.match(r"^https?://[^\s/]+", address) and not _valid_host(address):
            problems.append("Enter a URL such as https://example.com or a host name.")
    elif check_type == "dns":
        if not _valid_host(address) or _is_ip(address):
            problems.append("DNS checks need a host name to resolve, such as example.com.")
    elif not _valid_host(address):
        problems.append("Enter a valid IPv4/IPv6 address or host name.")
    return problems


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _valid_host(value: str) -> bool:
    if _is_ip(value):
        return True
    if re.fullmatch(r"[\d.]+", value):  # looks like an IPv4 address but is not one
        return False
    return bool(_HOSTNAME_RE.match(value))


class SettingsStore:
    """Thread-safe cached access to the ``settings`` table."""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {}
        self._listeners: list[Callable[[set[str]], None]] = []
        self.reload()

    def reload(self) -> None:
        values = dict(DEFAULTS)
        for row in self.db.query("SELECT key, value FROM settings"):
            if row["key"] in DEFAULTS:
                try:
                    values[row["key"]] = json.loads(row["value"])
                except ValueError:
                    pass
        with self._lock:
            self._cache = values

    def get(self, key: str) -> Any:
        with self._lock:
            return self._cache.get(key, DEFAULTS.get(key))

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._cache)

    def add_listener(self, callback: Callable[[set[str]], None]) -> None:
        self._listeners.append(callback)

    def update(self, changes: dict[str, Any], validate_all: bool = True) -> list[ValidationError]:
        """Persist ``changes`` atomically if the resulting settings are valid."""
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise KeyError(f"Unknown settings: {', '.join(sorted(unknown))}")
        merged = self.all()
        merged.update(changes)
        if validate_all:
            errors = validate(merged)
            if errors:
                return errors
        now = time.time()
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                [(k, json.dumps(v), now) for k, v in changes.items()],
            )
        with self._lock:
            self._cache.update(changes)
        for cb in list(self._listeners):
            cb(set(changes))
        return []

    def set(self, key: str, value: Any) -> None:
        """Set a single value that does not need cross validation (e.g. muted)."""
        self.update({key: value}, validate_all=False)
