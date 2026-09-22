"""Linux platform bits for Yes, Dev.

The Linux answer to what platform_mac.py does on macOS: where config and
logs live, how a single instance is enforced, and how autostart is
registered. No Accessibility grant exists on Linux - AT-SPI reads work
once toolkit-accessibility is on (gsettings: org.gnome.desktop.interface
toolkit-accessibility true).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Yes, Dev"
APP_SLUG = "yes-dev"


def _data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "YesDev"
    return Path.home() / ".local" / "share" / "YesDev"


DATA_DIR = _data_home()
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / f"{APP_SLUG}.log"
TRAY_LOG = DATA_DIR / "tray.log"

AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"
DESKTOP_PATH = AUTOSTART_DIR / "yes-dev.desktop"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


_lock_handle = None  # kept alive for the life of the process


def acquire_single_instance(name: str = "engine") -> bool:
    """True if we got the lock, False if another instance holds it."""
    global _lock_handle
    import fcntl

    ensure_data_dir()
    path = DATA_DIR / f".{name}.lock"
    handle = open(path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    try:
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass
    _lock_handle = handle
    return True


def is_wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


def atspi_available() -> tuple[bool, str]:
    """Whether gi.repository.Atspi imports. Returns (ok, detail)."""
    try:
        import gi  # noqa: F401

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401
    except Exception as exc:
        return False, (
            f"gi.repository.Atspi missing ({exc!r}). Install: "
            "sudo apt install python3-gi gir1.2-atspi-2.0 "
            "and run with /usr/bin/python3 (the distro python)."
        )
    return True, "ok"


_DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Name=Yes, Dev
Comment=Auto-approve Chrome's Allow remote debugging prompt
Exec={python} {script}
Path={workdir}
X-GNOME-Autostart-enabled=true
"""


def autostart_enabled() -> bool:
    return DESKTOP_PATH.exists()


def enable_autostart(script_path: Path) -> None:
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_PATH.write_text(
        _DESKTOP_TEMPLATE.format(
            python=sys.executable,
            script=str(Path(script_path).resolve()),
            workdir=str(Path(script_path).resolve().parent),
        ),
        encoding="utf-8",
    )
    try:
        DESKTOP_PATH.chmod(0o755)
    except OSError:
        pass


def disable_autostart() -> None:
    try:
        DESKTOP_PATH.unlink()
    except OSError:
        pass
