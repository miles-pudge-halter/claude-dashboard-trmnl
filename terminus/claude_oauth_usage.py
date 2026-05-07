"""Fetch Claude Code rate-limit data via the Anthropic OAuth usage API.

Replaces the upstream `claude_usage_scraper.py` which drove the Claude CLI's
TUI via pexpect+pyte — fragile and broke when the TUI's screen layout changed.
The OAuth API path is the same one steipete/codexbar uses (see
docs/claude.md in that repo): direct, structured, no PTY scraping.

Output shape matches the upstream scraper's output so sync_to_gist.py treats
both interchangeably:

    {
      "session":      {"pct": "40", "reset": "11pm"},
      "week_all":     {"pct": "93", "reset": "Mon 12pm"},
      "week_sonnet":  {"pct": "X",  "reset": "..."},
      "extra":        {"spent": "50.21", "limit": "50.00",
                       "pct": "100",     "reset": "..."}
    }

Token resolution (tried in order):
  1. macOS Keychain entry "Claude Code-credentials" via `security`.
  2. File ~/.claude/.credentials.json.
  3. Environment variable CLAUDE_OAUTH_TOKEN (lets the user override).

The token must have `user:profile` scope. CLI-only tokens with `user:inference`
cannot call /api/oauth/usage and will return 403.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

API_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"


def _from_keychain() -> Optional[str]:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _extract_access_token(result.stdout.strip())


def _from_file() -> Optional[str]:
    path = Path.home() / ".claude" / ".credentials.json"
    if not path.exists():
        return None
    try:
        return _extract_access_token(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _from_env() -> Optional[str]:
    token = os.environ.get("CLAUDE_OAUTH_TOKEN", "").strip()
    return token or None


def _extract_access_token(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    # The credentials blob can either be a JSON object containing
    # claudeAiOauth.accessToken, or already be the bare token string.
    if not raw.startswith("{"):
        return raw if raw.startswith("sk-ant-") else None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") or data.get("claude_ai_oauth") or data
    return (
        oauth.get("accessToken")
        or oauth.get("access_token")
        or None
    )


def resolve_token() -> str:
    for source in (_from_env, _from_keychain, _from_file):
        token = source()
        if token:
            return token
    sys.exit(
        "no Claude OAuth access token found. Tried CLAUDE_OAUTH_TOKEN env var, "
        "macOS keychain entry 'Claude Code-credentials', and ~/.claude/.credentials.json. "
        "Sign into Claude Code first or paste a token via the env var."
    )


def fetch_usage(token: str) -> dict:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "Accept": "application/json",
            "User-Agent": "claude-dashboard-terminus/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        sys.exit(f"oauth usage api {e.code}: {body}")
    except urllib.error.URLError as e:
        sys.exit(f"oauth usage api unreachable: {e}")


def _fmt_reset(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt_utc = datetime.fromisoformat(iso).astimezone(timezone.utc).astimezone()
    except ValueError:
        return iso
    now = datetime.now().astimezone()
    same_day = dt_utc.date() == now.date()
    hour = dt_utc.strftime("%I").lstrip("0") or "12"
    suffix = dt_utc.strftime("%p").lower()
    if same_day:
        return f"{hour}{suffix}"
    return dt_utc.strftime("%a ") + f"{hour}{suffix}"


def _pct(window: Optional[dict]) -> str:
    """The Anthropic OAuth API returns `utilization` already as a percent
    (0..100), not a fraction. `five_hour: 11.0` means 11%."""
    if not isinstance(window, dict):
        return "0"
    util = window.get("utilization")
    if util is None:
        return "0"
    return str(int(round(float(util))))


def _reset(window: Optional[dict]) -> str:
    if not isinstance(window, dict):
        return "—"
    return _fmt_reset(window.get("resets_at"))


def _money(value) -> str:
    """Money values come back as integer cents (`monthly_limit: 100000`
    means 1000 of whatever currency the account is in)."""
    if value is None:
        return "0"
    try:
        amount = float(value) / 100.0
    except (TypeError, ValueError):
        return "0"
    return f"{amount:.2f}".rstrip("0").rstrip(".") or "0"


def _iso(window: Optional[dict]) -> str:
    if not isinstance(window, dict):
        return ""
    return str(window.get("resets_at") or "")


def map_response(api: dict) -> dict:
    five_hour = api.get("five_hour") or {}
    seven_day = api.get("seven_day") or {}
    seven_day_sonnet = api.get("seven_day_sonnet") or {}
    extra = api.get("extra_usage") or {}

    return {
        "session": {
            "pct": _pct(five_hour),
            "reset": _reset(five_hour),
            "resets_at_iso": _iso(five_hour),
        },
        "week_all": {
            "pct": _pct(seven_day),
            "reset": _reset(seven_day),
            "resets_at_iso": _iso(seven_day),
        },
        "week_sonnet": {
            "pct": _pct(seven_day_sonnet),
            "reset": _reset(seven_day_sonnet),
            "resets_at_iso": _iso(seven_day_sonnet),
        },
        "extra": {
            "spent": _money(extra.get("used_credits")),
            "limit": _money(extra.get("monthly_limit")),
            "pct": _pct({"utilization": extra.get("utilization")}),
            "reset": _reset(extra),
            "currency": str(extra.get("currency") or "USD"),
        },
    }


def main() -> None:
    token = resolve_token()
    api = fetch_usage(token)
    print(json.dumps(map_response(api)))


if __name__ == "__main__":
    main()
