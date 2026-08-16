#!/usr/bin/env python3
"""Regenerate the host agent's Windows icon from the mark the tray already draws.

    uv run tools/build_hostagent_ico.py

Writes `graphics/logo/hostagent.ico`, which is what the desktop shortcut wears.

It calls `hostagent/console.py:write_icon_file` rather than drawing anything, so
the shortcut, the taskbar and the tray are the same picture by construction and
not by two implementations agreeing - the agent writes its own window icon with
the same call at startup. Both servers are passed as up: a shortcut is an
identity, not a status light, and a shortcut whose icon depended on when it was
last generated would be a lie the moment the backend stopped.

The console module is loaded by path because it lives outside the package - it
runs on the Windows host, not in the container - which is the same reason
`tests/test_hostagent_console.py` loads it that way.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow", "pystray", "textual"]
# ///

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "hostagent" / "console.py"
OUT = ROOT / "graphics" / "logo" / "hostagent.ico"


def load_console():
    spec = importlib.util.spec_from_file_location("agent_console", CONSOLE)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {CONSOLE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_console"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    console = load_console()
    colors = console.icon_colors(
        {"router": {"listening": True}, "embed": {"listening": True}}, ["router", "embed"]
    )
    console.write_icon_file(OUT, colors)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(console.ICON_FILE_SIZES)} sizes)")


if __name__ == "__main__":
    main()
