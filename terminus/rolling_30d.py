"""Sum cost + tokens for the rolling 30 days from ~/.claude/projects/**/*.jsonl.

Upstream session_stats reports calendar month (resets on the 1st), not the
rolling 30-day window codex bar surfaces. This scanner mirrors session_stats's
per-message parsing closely (same schema assumptions, same _cost / _tier
helpers, same desktop-config patching from sync_to_gist) so the numbers stay
consistent — only the time bound differs.

Output keys are namespaced under `last_30d_*` so they don't shadow upstream's
`month_*` fields, which we may still want to surface elsewhere.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "launchd-setup"))

import session_stats  # noqa: E402


def scan() -> dict:
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    cost = 0.0
    inp_total = out_total = cr_total = cw_total = msgs = 0
    daily_costs: dict[str, float] = {}
    days_with_activity: set[str] = set()

    projects_dir = session_stats.PROJECTS_DIR
    if not projects_dir.exists():
        return _empty()

    for jsonl in projects_dir.rglob("*.jsonl"):
        try:
            with open(jsonl, "r", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    m = e.get("message")
                    if not isinstance(m, dict) or m.get("role") != "assistant":
                        continue
                    u = m.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = e.get("timestamp", "")
                    day = ts[:10] if ts else ""
                    if not day or day < cutoff_date:
                        continue
                    inp = u.get("input_tokens", 0)
                    out = u.get("output_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    cw = u.get("cache_creation_input_tokens", 0)
                    speed = u.get("speed", "")
                    ws = (u.get("server_tool_use") or {}).get("web_search_requests", 0)
                    model = m.get("model", "claude-sonnet-4-6")
                    c = session_stats._cost(model, inp, out, cr, cw, speed, ws)
                    cost += c
                    inp_total += inp
                    out_total += out
                    cr_total += cr
                    cw_total += cw
                    msgs += 1
                    daily_costs[day] = daily_costs.get(day, 0.0) + c
                    days_with_activity.add(day)
        except OSError:
            continue

    tokens = inp_total + out_total + cr_total + cw_total
    days_seen = max(1, len(days_with_activity))
    avg_per_active_day = cost / days_seen
    avg_per_calendar_day = cost / 30.0

    # Forward-projection: assume the next 30 days look like the past 30. This
    # matches codex bar's "lasts until reset" framing in spirit — pace * window.
    proj = avg_per_calendar_day * 30

    return {
        "last_30d_cost": session_stats._fc(cost),
        "last_30d_cost_raw": str(round(cost, 2)),
        "last_30d_tokens": session_stats._ft(tokens),
        "last_30d_tokens_raw": str(tokens),
        "last_30d_msgs": str(msgs),
        "last_30d_active_days": str(days_seen),
        "last_30d_avg": session_stats._fc(avg_per_calendar_day),
        "last_30d_avg_active": session_stats._fc(avg_per_active_day),
        "last_30d_proj": session_stats._fc(proj),
    }


def _empty() -> dict:
    return {
        "last_30d_cost": "0",
        "last_30d_cost_raw": "0",
        "last_30d_tokens": "0",
        "last_30d_tokens_raw": "0",
        "last_30d_msgs": "0",
        "last_30d_active_days": "0",
        "last_30d_avg": "0",
        "last_30d_avg_active": "0",
        "last_30d_proj": "0",
    }


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2))
