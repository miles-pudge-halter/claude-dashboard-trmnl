"""Claude Code session scanner aligned with steipete/codexbar.

Replaces the upstream `session_stats.gather_stats()` call in our pipeline
with a parser that:
  1. dedupes streaming chunks by (messageId, requestId) — keeping the last
     cumulative state per assistant message rather than summing every
     intermediate write (Claude Code's JSONL contains many rows per
     message during streaming, each with cumulative usage),
  2. uses codex bar's per-token pricing tables — no `opus_fast` 6× tier,
     and applies the 200K-token threshold uplift for sonnet 4.5 / 4.6,
  3. checks both `~/.claude/projects` and `~/.config/claude/projects`,
     matching codex bar's `defaultClaudeProjectsRoots`.

Output keys match `session_stats.gather_stats()` so the gist payload, the
merger, and the template all continue to work without changes — the
numbers just become consistent with codex bar instead of session_stats.

Plugin and MCP detection is delegated to `session_stats._plugins()` and
`_mcps()` (data sources outside the JSONL files, no pricing concern).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "launchd-setup"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_stats  # noqa: E402  — only for _plugins / _mcps
from claude_cost import claude_cost_usd  # noqa: E402


def _project_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".claude" / "projects",
        home / ".config" / "claude" / "projects",
    ]
    return [p for p in candidates if p.exists()]


def _format_currency(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def _format_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B".replace(".0B", "B")
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return str(value)


def _project_name(directory_name: str) -> str:
    parts = directory_name.lstrip("-").split("-")
    meaningful = [p for p in parts if p not in {"Users", "private", "tmp"} and len(p) > 1]
    if meaningful:
        return meaningful[-1]
    return parts[-1] if parts else directory_name


def _iter_deduped_rows(jsonl_path: Path) -> Iterable[dict]:
    """Yield one row per assistant message in `jsonl_path`, with streaming
    chunks collapsed to their last cumulative state.

    Each yielded row is a dict containing the model, timestamp, day key,
    and the four token counts. Lines without a `(messageId, requestId)`
    pair (older logs) are passed through unkeyed — codex bar keeps these
    as separate rows to avoid dropping data.
    """
    keyed: dict[str, dict] = {}
    unkeyed: list[dict] = []
    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = e.get("message")
                if not isinstance(m, dict) or m.get("role") != "assistant":
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                inp = max(0, int(u.get("input_tokens", 0) or 0))
                out = max(0, int(u.get("output_tokens", 0) or 0))
                cr = max(0, int(u.get("cache_read_input_tokens", 0) or 0))
                cw = max(0, int(u.get("cache_creation_input_tokens", 0) or 0))
                if inp == 0 and out == 0 and cr == 0 and cw == 0:
                    continue
                ts = str(e.get("timestamp", "") or "")
                day = ts[:10] if ts else ""
                row = {
                    "model": m.get("model", "claude-sonnet-4-6"),
                    "ts": ts,
                    "day": day,
                    "inp": inp,
                    "out": out,
                    "cr": cr,
                    "cw": cw,
                }
                msg_id = m.get("id")
                req_id = e.get("requestId")
                if msg_id and req_id:
                    keyed[f"{msg_id}:{req_id}"] = row
                else:
                    unkeyed.append(row)
    except OSError:
        return
    yield from keyed.values()
    yield from unkeyed


def _model_tier(model: str) -> str:
    m = model.lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    return "sonnet"


def gather_stats() -> dict:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago_day = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    last_30d_cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    active_cutoff_ts = (now - timedelta(minutes=60)).isoformat()

    today_cost = yesterday_cost = week_cost = month_cost = all_time_cost = 0.0
    last_30d_cost = 0.0
    today_in = today_out = today_cr = today_cw = today_msgs = 0
    week_tokens = week_msgs = 0
    last_30d_tokens = last_30d_msgs = 0
    last_30d_days: set[str] = set()
    days_with_activity: set[str] = set()
    daily_costs: dict[str, float] = {}
    model_costs: dict[str, float] = {}
    model_tokens: dict[str, int] = {}
    model_msgs: dict[str, int] = {}
    project_costs: dict[str, float] = {}
    project_sessions: dict[str, set[str]] = {}
    today_first_ts: str | None = None
    today_last_ts: str | None = None
    longest_session_msgs = 0
    sessions_today_set: set[str] = set()
    week_sessions_set: set[str] = set()
    month_sessions_set: set[str] = set()
    all_sessions_set: set[str] = set()
    active_now_set: set[str] = set()
    yesterday_in = yesterday_out = yesterday_cr = yesterday_cw = 0  # for cache_savings parity

    for root in _project_roots():
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            project = _project_name(project_dir.name)
            for jsonl_path in project_dir.rglob("*.jsonl"):
                session_id = jsonl_path.stem
                rows = list(_iter_deduped_rows(jsonl_path))
                if not rows:
                    continue
                all_sessions_set.add(session_id)
                session_msg_count = 0
                file_cost = 0.0
                for row in rows:
                    cost = claude_cost_usd(
                        row["model"], row["inp"], row["cr"], row["cw"], row["out"]
                    ) or 0.0
                    total_tokens = row["inp"] + row["out"] + row["cr"] + row["cw"]
                    tier = _model_tier(row["model"])
                    model_costs[tier] = model_costs.get(tier, 0.0) + cost
                    model_tokens[tier] = model_tokens.get(tier, 0) + total_tokens
                    model_msgs[tier] = model_msgs.get(tier, 0) + 1
                    all_time_cost += cost
                    session_msg_count += 1
                    file_cost += cost
                    day = row["day"]
                    if day:
                        daily_costs[day] = daily_costs.get(day, 0.0) + cost
                        days_with_activity.add(day)
                        if day >= last_30d_cutoff:
                            last_30d_cost += cost
                            last_30d_tokens += total_tokens
                            last_30d_msgs += 1
                            last_30d_days.add(day)
                        if day >= week_ago_day:
                            week_cost += cost
                            week_tokens += total_tokens
                            week_msgs += 1
                            week_sessions_set.add(session_id)
                        if day >= month_start:
                            month_cost += cost
                            month_sessions_set.add(session_id)
                        if day == today:
                            today_cost += cost
                            today_in += row["inp"]
                            today_out += row["out"]
                            today_cr += row["cr"]
                            today_cw += row["cw"]
                            today_msgs += 1
                            sessions_today_set.add(session_id)
                            ts = row["ts"]
                            if ts:
                                if today_first_ts is None or ts < today_first_ts:
                                    today_first_ts = ts
                                if today_last_ts is None or ts > today_last_ts:
                                    today_last_ts = ts
                                if ts > active_cutoff_ts:
                                    active_now_set.add(session_id)
                        elif day == yesterday:
                            yesterday_cost += cost
                            yesterday_in += row["inp"]
                            yesterday_out += row["out"]
                            yesterday_cr += row["cr"]
                            yesterday_cw += row["cw"]
                if file_cost > 0:
                    project_costs[project] = project_costs.get(project, 0.0) + file_cost
                    project_sessions.setdefault(project, set()).add(session_id)
                if session_msg_count > longest_session_msgs:
                    longest_session_msgs = session_msg_count

    # Derived metrics
    today_tokens = today_in + today_out + today_cr + today_cw
    total_input_today = today_in + today_cr + today_cw
    cache_pct = (
        round(today_cr / total_input_today * 100) if total_input_today > 0 else 0
    )
    # Cache savings = what today's traffic would have cost if every cached read
    # had been a fresh input. Use opus-4-6 as the worst-case reference model.
    no_cache_cost = (
        claude_cost_usd("claude-opus-4-6", total_input_today, 0, 0, today_out) or 0.0
    ) if total_input_today > 0 else 0.0
    cache_savings = max(0.0, no_cache_cost - today_cost)
    cost_per_req = today_cost / today_msgs if today_msgs > 0 else 0.0
    cost_trend = "—"
    if yesterday_cost > 0:
        ch = ((today_cost - yesterday_cost) / yesterday_cost) * 100
        cost_trend = f"+{ch:.0f}%" if ch > 0 else f"{ch:.0f}%"
    days_in_month = now.day
    daily_avg_month = month_cost / days_in_month if days_in_month > 0 else 0.0
    projected_cost = month_cost + daily_avg_month * max(0, 30 - days_in_month)
    avg_session_cost = (
        month_cost / len(month_sessions_set) if month_sessions_set else 0.0
    )

    # Streak: consecutive days back from today with at least one session
    streak = 0
    for i in range(60):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in days_with_activity:
            streak += 1
        else:
            break
    active_days_30 = len([d for d in days_with_activity if d >= last_30d_cutoff])

    # Today coding hours
    hours_today = "—"
    if today_first_ts and today_last_ts:
        try:
            t0 = datetime.fromisoformat(today_first_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(today_last_ts.replace("Z", "+00:00"))
            mins = max(0.0, (t1 - t0).total_seconds() / 60.0)
            hours_today = f"{mins / 60:.1f}h" if mins >= 60 else f"{int(mins)}m"
        except ValueError:
            pass

    # Top project by cost
    if project_costs:
        top = max(project_costs, key=project_costs.get)
        top_project = top
        top_proj_cost = _format_currency(project_costs[top])
    else:
        top_project = "—"
        top_proj_cost = "0"

    # Model breakdown — display by tokens (matches session_stats's behavior)
    total_msgs = sum(model_msgs.values()) or 1
    primary = max(model_msgs, key=model_msgs.get) if model_msgs else "—"
    primary_pct = round(model_msgs.get(primary, 0) / total_msgs * 100) if model_msgs else 0
    total_tokens_all_models = sum(model_tokens.values()) or 1
    model_line_parts = [
        f"{tier}:{model_msgs[tier]}"
        for tier in ("opus", "sonnet", "haiku")
        if tier in model_msgs and model_msgs[tier] / total_msgs >= 0.01
    ]
    model_line = " / ".join(model_line_parts) or (
        f"{primary}:{model_msgs.get(primary, 0)}" if primary != "—" else "—"
    )

    # 7-day chart — newest day at d6
    day_data: list[tuple[str, float]] = []
    max_dc = 0.01
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        c = daily_costs.get(d, 0.0)
        lbl = (now - timedelta(days=i)).strftime("%a")[0]
        day_data.append((lbl, c))
        if c > max_dc:
            max_dc = c

    plugin_count = session_stats._plugins()  # noqa: SLF001
    mcp_count, mcp_names = session_stats._mcps()  # noqa: SLF001

    result = {
        "today_cost": _format_currency(today_cost),
        "yesterday_cost": _format_currency(yesterday_cost),
        "week_cost": _format_currency(week_cost),
        "month_cost": _format_currency(month_cost),
        "all_time_cost": _format_currency(all_time_cost),
        "projected_cost": _format_currency(projected_cost),
        "daily_avg": f"{daily_avg_month:.2f}",
        "cost_trend": cost_trend,
        "cost_per_req": f"{cost_per_req:.2f}",
        "avg_session_cost": f"{avg_session_cost:.2f}",
        "cache_savings": _format_currency(cache_savings),

        "today_tokens": _format_tokens(today_tokens),
        "today_input": _format_tokens(today_in),
        "today_output": _format_tokens(today_out),
        "today_cache_read": _format_tokens(today_cr),
        "today_cache_write": _format_tokens(today_cw),
        "cache_pct": str(cache_pct),
        "today_requests": str(today_msgs),
        "today_msgs": str(today_msgs),

        "week_tokens": _format_tokens(week_tokens),
        "week_msgs": str(week_msgs),
        "week_sessions": str(len(week_sessions_set)),

        "active_now": str(len(active_now_set)),
        "sessions_today": str(len(sessions_today_set)),
        "month_sessions": str(len(month_sessions_set)),
        "all_sessions": str(len(all_sessions_set)),
        "active_days": str(active_days_30),
        "streak": str(streak),
        "hours_today": hours_today,
        "longest_session": str(longest_session_msgs),

        "primary_model": primary,
        "primary_pct": str(primary_pct),
        "model_line": model_line,
        "opus_tokens": _format_tokens(model_tokens.get("opus", 0)),
        "opus_pct": str(round(model_tokens.get("opus", 0) / total_tokens_all_models * 100)),
        "sonnet_tokens": _format_tokens(model_tokens.get("sonnet", 0)),
        "sonnet_pct": str(round(model_tokens.get("sonnet", 0) / total_tokens_all_models * 100)),
        "haiku_tokens": _format_tokens(model_tokens.get("haiku", 0)),
        "haiku_pct": str(round(model_tokens.get("haiku", 0) / total_tokens_all_models * 100)),

        "top_project": top_project,
        "top_proj_cost": top_proj_cost,

        "plugin_count": str(plugin_count),
        "mcp_count": str(mcp_count),
        "mcp_names": mcp_names,

        # Rolling 30-day — these used to live in rolling_30d.py; folded here.
        "last_30d_cost": _format_currency(last_30d_cost),
        "last_30d_cost_raw": str(round(last_30d_cost, 2)),
        "last_30d_tokens": _format_tokens(last_30d_tokens),
        "last_30d_tokens_raw": str(last_30d_tokens),
        "last_30d_msgs": str(last_30d_msgs),
        "last_30d_active_days": str(len(last_30d_days)),
        "last_30d_avg": _format_currency(last_30d_cost / 30.0),
        "last_30d_avg_active": _format_currency(
            last_30d_cost / max(1, len(last_30d_days))
        ),
        "last_30d_proj": _format_currency(last_30d_cost),
    }

    # 7-day chart fields
    for i, (lbl, c) in enumerate(day_data):
        pct = min(100, round(c / max_dc * 100)) if max_dc > 0 else 0
        result[f"d{i}_lbl"] = lbl
        result[f"d{i}_pct"] = str(pct)
        result[f"d{i}_cost"] = _format_currency(c)

    return result


if __name__ == "__main__":
    print(json.dumps(gather_stats(), indent=2))
