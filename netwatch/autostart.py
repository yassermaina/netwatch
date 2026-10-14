"""XDG autostart entry management (explicit user opt-in)."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from netwatch import APP_ID, APP_NAME
from netwatch.paths import autostart_dir


def autostart_file() -> Path:
    return autostart_dir() / f"{APP_ID}.desktop"


def launch_command() -> str:
    installed = shutil.which("netwatch")
    if installed:
        return f"{shlex.quote(installed)} --background"
    return f"{shlex.quote(sys.executable)} -m netwatch --background"


def autostart_enabled() -> bool:
    return autostart_file().exists()


def set_autostart(enabled: bool) -> None:
    path = autostart_file()
    if not enabled:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Start network monitoring in the background\n"
        f"Exec={launch_command()}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=5\n"
        "NoDisplay=true\n"
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
