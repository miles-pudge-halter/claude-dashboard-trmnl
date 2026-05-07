"""Sync Claude Code dashboard stats to a private GitHub gist.

Runs the upstream session_stats.gather_stats() (cross-platform safe — only file I/O),
adds Terminus-specific fields, and updates a GitHub gist via the `gh` CLI so a
Terminus Poll extension can fetch the JSON over HTTPS.

Designed to be invoked by Windows Task Scheduler every ~15 minutes.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "launchd-setup"))

import session_stats  # noqa: E402

# Override macOS-only desktop config path for Windows / Linux.
if platform.system() == "Windows":
    session_stats.DESKTOP_CONFIG = (
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        / "Claude"
        / "claude_desktop_config.json"
    )
elif platform.system() == "Linux":
    session_stats.DESKTOP_CONFIG = (
        Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    )


GIST_ID = os.environ.get("CLAUDE_DASHBOARD_GIST_ID", "").strip()
GIST_FILENAME = "data.json"


def merge_payload() -> dict:
    stats = session_stats.gather_stats()

    # Fields normally produced by claude_usage_scraper.py on macOS, which uses
    # pexpect+pyte against the Claude CLI's TUI /usage screen. That stack does
    # not work on Windows, so emit zeroed placeholders here. The Terminus
    # template hides the relevant rows when these are absent.
    rate_limit_defaults = {
        "extra_spent": "0",
        "extra_limit": "0",
        "extra_pct": "0",
        "extra_reset": "—",
        "session_pct": "0",
        "session_reset_short": "—",
        "week_all_pct": "0",
        "week_all_reset_short": "—",
        "week_sonnet_pct": "0",
        "week_sonnet_reset_short": "—",
        "has_rate_limits": False,
    }

    return {
        **rate_limit_defaults,
        **stats,
        "updated_at": datetime.now().strftime("%b %d at %I:%M%p").replace(" 0", " "),
    }


def push_to_gist(payload: dict) -> None:
    if not GIST_ID:
        sys.stderr.write(
            "CLAUDE_DASHBOARD_GIST_ID is not set. "
            "Create a gist with `gh gist create data.json -d 'claude code dashboard'` "
            "and set the ID in the environment.\n"
        )
        sys.exit(2)

    body = json.dumps(payload, indent=2)
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "claude_dashboard_data.json"
    tmp.write_text(body, encoding="utf-8")

    gh = os.environ.get("GH_BIN") or r"C:\Program Files\GitHub CLI\gh.exe"
    if not Path(gh).exists():
        gh = "gh"

    result = subprocess.run(
        [gh, "gist", "edit", GIST_ID, str(tmp), "--filename", GIST_FILENAME],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        sys.stderr.write(f"gh gist edit failed ({result.returncode}):\n{result.stderr}\n")
        sys.exit(result.returncode)

    print(f"gist {GIST_ID} updated ({len(body)} bytes)")


if __name__ == "__main__":
    push_to_gist(merge_payload())
