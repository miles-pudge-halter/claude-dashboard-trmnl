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
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def _strip_tz(value: str) -> str:
    return re.sub(r"\s*\(.*?\)\s*$", "", str(value))


def _run_usage_source(script_path: Path, label: str) -> Optional[dict]:
    """Subprocess a JSON-on-stdout usage source script and parse its output."""
    if not script_path.exists():
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        sys.stderr.write(f"{label} invocation failed: {e}\n")
        return None
    if result.returncode != 0:
        sys.stderr.write(
            f"{label} exited {result.returncode}: {result.stderr.strip()[:400]}\n"
        )
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"{label} output not JSON ({e}): {result.stdout[:200]!r}\n")
        return None


def maybe_run_scraper() -> dict:
    """Resolve rate-limit data for the gist payload.

    Order:
      1. claude_oauth_usage.py — direct OAuth call to Anthropic's usage API,
         the same path steipete/codexbar uses. Cross-platform; preferred.
      2. launchd-setup/claude_usage_scraper.py — pexpect+pyte against the
         Claude CLI TUI; macOS only and brittle, kept as a fallback for
         environments where the OAuth token lacks `user:profile` scope.

    Returns a flat dict ready to merge into the gist payload, or {} if both
    sources are unavailable.
    """
    here = Path(__file__).resolve().parent
    metrics: Optional[dict] = None
    for script_path, label in (
        (here / "claude_oauth_usage.py", "oauth-usage"),
        (ROOT / "launchd-setup" / "claude_usage_scraper.py", "tui-scraper"),
    ):
        candidate = _run_usage_source(script_path, label)
        if candidate and any(
            isinstance(v, dict) and v.get("pct", "0") != "0"
            for v in candidate.values()
        ):
            metrics = candidate
            break
        if candidate and metrics is None:
            metrics = candidate  # zero values, but better than nothing if no other source works

    if metrics is None:
        return {}

    def _get(key: str, field: str, default: str = "—") -> str:
        return str(metrics.get(key, {}).get(field, default))

    session_reset = _get("session", "reset")
    week_all_reset = _get("week_all", "reset")
    week_sonnet_reset = _get("week_sonnet", "reset")
    extra_reset = _get("extra", "reset")

    return {
        "session_pct": _get("session", "pct", "0"),
        "session_reset": session_reset,
        "session_reset_short": _strip_tz(session_reset),
        "session_resets_at_iso": _get("session", "resets_at_iso", ""),
        "week_all_pct": _get("week_all", "pct", "0"),
        "week_all_reset": week_all_reset,
        "week_all_reset_short": _strip_tz(week_all_reset),
        "week_all_resets_at_iso": _get("week_all", "resets_at_iso", ""),
        "week_sonnet_pct": _get("week_sonnet", "pct", "0"),
        "week_sonnet_reset": week_sonnet_reset,
        "week_sonnet_reset_short": _strip_tz(week_sonnet_reset),
        "week_sonnet_resets_at_iso": _get("week_sonnet", "resets_at_iso", ""),
        "extra_pct": _get("extra", "pct", "0"),
        "extra_spent": _get("extra", "spent", "0"),
        "extra_limit": _get("extra", "limit", "0"),
        "extra_reset": _strip_tz(extra_reset),
        "extra_currency": _get("extra", "currency", "USD"),
        "has_rate_limits": True,
    }


def merge_payload() -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    # Use our codex-bar-aligned scanner instead of session_stats.gather_stats():
    # the upstream version sums every JSONL line including streaming chunks
    # with cumulative token counts (3-5× over-count) and applies a 6× cost
    # multiplier for opus-4-6 fast mode that codex bar doesn't. scanner.py
    # produces the same dict shape so downstream code is unchanged.
    import scanner  # noqa: WPS433
    stats = scanner.gather_stats()

    # Zeroed placeholders for the rate-limit fields. Overridden by the scraper
    # output below when running on a machine with pexpect+pyte (macOS / Linux).
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

    rate_limits = maybe_run_scraper()

    return {
        **rate_limit_defaults,
        **rate_limits,
        **stats,
        "updated_at": datetime.now().strftime("%b %d at %I:%M%p").replace(" 0", " "),
        "updated_at_iso": datetime.now().astimezone().isoformat(),
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
