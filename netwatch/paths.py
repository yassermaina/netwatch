"""XDG base directory handling and the small bootstrap config file.

Most preferences live in the SQLite ``settings`` table.  The database location
itself cannot live inside the database, so it is stored in
``$XDG_CONFIG_HOME/netwatch/config.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = "netwatch"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_DIR


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_DIR


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_DIR


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_DIR


def autostart_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart"


def default_reports_dir() -> Path:
    docs = None
    try:
        from gi.repository import GLib  # noqa: PLC0415

        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
    except Exception:  # GLib unavailable (tests) or no XDG user dirs
        docs = None
    base = Path(docs) if docs else Path.home()
    return base / "NetWatch Reports"


def _bootstrap_file() -> Path:
    return config_dir() / "config.json"


def read_bootstrap() -> dict:
    try:
        with open(_bootstrap_file(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_bootstrap(data: dict) -> None:
    path = _bootstrap_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def database_path(override: str | None = None) -> Path:
    """Resolve the database path: CLI override > $NETWATCH_DB > config.json > default."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get("NETWATCH_DB")
    if env:
        return Path(env).expanduser()
    configured = read_bootstrap().get("database_path")
    if configured:
        return Path(configured).expanduser()
    return data_dir() / "netwatch.db"
