"""Writable resource paths for installed Windows and Linux applications."""

import os
from pathlib import Path
import shutil
import sys


def user_resource_path(filename: str) -> str:
    """Keep development paths; preserve frozen app settings across upgrades."""
    bundled = Path(__file__).resolve().parent / "resources" / filename
    if not getattr(sys, "frozen", False):
        return str(bundled)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        destination = base / "JMStudio" / filename
    elif sys.platform.startswith("linux"):
        configured = Path(os.environ.get("XDG_CONFIG_HOME", ""))
        base = configured if configured.is_absolute() else Path.home() / ".config"
        destination = base / "jm-studio" / filename
    else:
        return str(bundled)
    if not destination.exists() and bundled.is_file():
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation preserves an existing user's settings.
            with destination.open("xb") as output, bundled.open("rb") as source:
                shutil.copyfileobj(source, output)
        except FileExistsError:
            pass
        except OSError:
            # Store readers/writers already handle unavailable files gracefully.
            pass
    return str(destination)
