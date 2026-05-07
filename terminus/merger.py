"""Merge Claude Code dashboard JSONs from multiple machines into one gist.

Designed to run on the Hetzner box every 15 minutes via cron, fetching the per-
machine gists pushed by sync_to_gist.py from each developer machine.

Reads config from /etc/terminus-dashboard.env (mode 600, root-only):

    SOURCE_URLS=https://gist.githubusercontent.com/.../raw/data.json,https://gist.githubusercontent.com/.../raw/data.json
    MERGED_GIST_ID=<id of the gist to write to>
    MERGED_GIST_FILENAME=data.json
    GH_TOKEN=ghp_xxx (gist scope only)

The script:
  1. Fetches each source URL with Cache-Control: no-cache (bypass GitHub raw CDN).
  2. Parses formatted strings back to raw numbers, sums/maxes/recomputes per the
     field-policy table below.
  3. Reformats for display.
  4. PATCHes the merged gist via the GitHub API.

Field policy:

    sum:        all $-prefixed cost fields, token fields, message/request counts,
                session counts, cache_savings, tokens-by-model, day-bucket cost.
    max:        streak, longest_session, active_days, hours_today.
    recomputed: cache_pct, cost_per_req, daily_avg, primary_pct, primary_model.
    set-union:  mcp_names, top_project (comma-separated, deduped).
    newest:     updated_at.
    constants:  d0_lbl..d6_lbl pass through unchanged (day-of-week markers).

Known imprecision: max(streak) is incorrect when activity is spread across
machines on different days. Fixing requires per-day activity data, which
session_stats does not expose. Document this and revisit if it bites.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path("/etc/terminus-dashboard.env")
DEFAULT_OUTPUT_PATH = "/opt/terminus-dashboard/merged.json"


def load_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        sys.exit(f"missing config: {CONFIG_PATH}")
    out: dict[str, str] = {}
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    for required in ("SOURCE_URLS",):
        if not out.get(required):
            sys.exit(f"config missing {required}")
    out.setdefault("MERGED_GIST_FILENAME", "data.json")
    out.setdefault("MERGED_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
    return out


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Format parsing — invert session_stats's _fc / _ft helpers.
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_currency(value: str) -> float:
    """`'1.9k'` → 1900.0; `'258'` → 258.0; `'$258'` → 258.0; `'0.00'` → 0.0."""
    if value is None:
        return 0.0
    s = str(value).strip().lstrip("$").replace(",", "")
    if not s or s in ("—", "-", "—"):
        return 0.0
    multiplier = 1.0
    if s.endswith(("k", "K")):
        multiplier = 1_000
        s = s[:-1]
    elif s.endswith(("m", "M")):
        multiplier = 1_000_000
        s = s[:-1]
    elif s.endswith(("b", "B")):
        multiplier = 1_000_000_000
        s = s[:-1]
    try:
        return float(s) * multiplier
    except ValueError:
        return 0.0


def parse_tokens(value: str) -> int:
    """`'112.6M'` → 112_600_000; `'9K'` → 9_000; `'788'` → 788."""
    return int(parse_currency(value))


def parse_int(value: str) -> int:
    if value is None:
        return 0
    s = str(value).strip()
    if not s or s in ("—", "-", "—"):
        return 0
    m = _NUM_RE.search(s.replace(",", ""))
    return int(float(m.group(0))) if m else 0


def parse_float(value: str) -> float:
    return parse_currency(value)


def fmt_currency(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def fmt_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B".replace(".0B", "B")
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return str(value)


def fmt_int(value: int) -> str:
    return str(int(value))


def _parse_hours_minutes(value) -> float:
    """Return hours from `'21m'`, `'4.1h'`, `'4.0h'`, `'—'`, `'0'`."""
    s = str(value).strip()
    if not s or s in ("—", "-"):
        return 0.0
    if s.endswith("m") and not s.endswith("am") and not s.endswith("pm"):
        try:
            return float(s[:-1].strip()) / 60.0
        except ValueError:
            return 0.0
    if s.endswith("h"):
        try:
            return float(s[:-1].strip())
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fmt_hours_minutes(hours: float) -> str:
    if hours <= 0:
        return "—"
    if hours < 1:
        return f"{int(round(hours * 60))}m"
    return f"{hours:.1f}h"


# ---------------------------------------------------------------------------
# Merge.
# ---------------------------------------------------------------------------

CURRENCY_SUMS = (
    "today_cost",
    "yesterday_cost",
    "week_cost",
    "month_cost",
    "all_time_cost",
    "projected_cost",
    "cache_savings",
    "top_proj_cost",
)
TOKEN_SUMS = (
    "today_tokens",
    "today_input",
    "today_output",
    "today_cache_read",
    "today_cache_write",
    "week_tokens",
    "opus_tokens",
    "sonnet_tokens",
    "haiku_tokens",
)
INT_SUMS = (
    "today_msgs",
    "today_requests",
    "week_msgs",
    "week_sessions",
    "active_now",
    "sessions_today",
    "month_sessions",
    "all_sessions",
    "plugin_count",
    "mcp_count",
)
INT_MAXES = ("streak", "active_days", "longest_session")
DAY_BUCKETS = tuple(f"d{i}" for i in range(7))


RATE_LIMIT_FIELDS = (
    "extra_spent",
    "extra_limit",
    "extra_pct",
    "extra_reset",
    "extra_currency",
    "session_pct",
    "session_reset",
    "session_reset_short",
    "session_resets_at_iso",
    "week_all_pct",
    "week_all_reset",
    "week_all_reset_short",
    "week_all_resets_at_iso",
    "week_sonnet_pct",
    "week_sonnet_reset",
    "week_sonnet_reset_short",
    "week_sonnet_resets_at_iso",
)

ROLLING_30D_RAW_FIELDS = (
    "last_30d_cost_raw",
    "last_30d_tokens_raw",
    "last_30d_msgs",
    "last_30d_active_days",
)

# Total seconds per rate-limit window — used to recompute pace at merge time
# from resets_at_iso. Anthropic's API names: five_hour, seven_day.
WINDOW_SECONDS = {
    "session": 5 * 3600,
    "week_all": 7 * 24 * 3600,
    "week_sonnet": 7 * 24 * 3600,
}


def _parse_iso(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _fmt_eta(seconds: float) -> str:
    if seconds <= 0:
        return "now"
    secs = int(seconds)
    if secs >= 86400:
        days = secs // 86400
        hours = (secs % 86400) // 3600
        return f"{days}d {hours}h" if hours else f"{days}d"
    if secs >= 3600:
        hours = secs // 3600
        mins = (secs % 3600) // 60
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    if secs >= 60:
        return f"{secs // 60}m"
    return f"{secs}s"


def _enrich_window(out: dict, prefix: str, total_seconds: int, now_utc: datetime) -> None:
    """Populate `<prefix>_pct_left`, `_eta`, `_pace_pct`, `_reserve_pct` for one window.

    Reads the source fields (`<prefix>_pct`, `<prefix>_resets_at_iso`) already
    on `out` and computes the derived values at merge time so the ETA stays
    fresh on each cron tick rather than drifting from the per-machine fetch.
    """
    try:
        pct = int(out.get(f"{prefix}_pct", "0") or "0")
    except ValueError:
        pct = 0
    out[f"{prefix}_pct_left"] = str(max(0, 100 - pct))

    reset_dt = _parse_iso(out.get(f"{prefix}_resets_at_iso", ""))
    if not reset_dt:
        out[f"{prefix}_eta"] = "—"
        out[f"{prefix}_pace_pct"] = "0"
        out[f"{prefix}_reserve_pct"] = "0"
        out[f"{prefix}_runway_eta"] = "—"
        return

    seconds_to_reset = (reset_dt - now_utc).total_seconds()
    out[f"{prefix}_eta"] = _fmt_eta(seconds_to_reset)

    elapsed = max(0.0, total_seconds - max(0.0, seconds_to_reset))
    pace_pct = int(round(elapsed / total_seconds * 100)) if total_seconds else 0
    out[f"{prefix}_pace_pct"] = str(pace_pct)

    pct_left = 100 - pct
    reserve = pct_left + pace_pct - 100  # > 0 means ahead of pace
    out[f"{prefix}_reserve_pct"] = str(max(0, reserve))

    # Runway: at the current burn rate, how long does the remaining quota last?
    # If you're on or ahead of pace, runway lasts to reset. If behind, runway
    # is shorter — `pct_left` divided by burn_rate (used / elapsed) gives time
    # before exhaustion.
    if pct == 0:
        out[f"{prefix}_runway_eta"] = _fmt_eta(seconds_to_reset)
    elif elapsed <= 0:
        out[f"{prefix}_runway_eta"] = _fmt_eta(seconds_to_reset)
    else:
        burn_per_sec = pct / elapsed
        if burn_per_sec <= 0:
            out[f"{prefix}_runway_eta"] = _fmt_eta(seconds_to_reset)
        else:
            secs_to_zero = pct_left / burn_per_sec
            if secs_to_zero >= seconds_to_reset:
                out[f"{prefix}_runway_eta"] = _fmt_eta(seconds_to_reset)
            else:
                out[f"{prefix}_runway_eta"] = _fmt_eta(secs_to_zero)


def merge(payloads: list[dict]) -> dict:
    if not payloads:
        return {}

    out: dict = {}

    # Pass through rate-limit fields from whichever payload has has_rate_limits.
    # These fields aren't sensibly summed (extra_pct is per-account, not per
    # machine), so first-wins from a payload that ran the scraper.
    rate_limit_source = next(
        (p for p in payloads if p.get("has_rate_limits") is True), None
    )
    if rate_limit_source:
        for k in RATE_LIMIT_FIELDS:
            if k in rate_limit_source:
                out[k] = rate_limit_source[k]
        out["has_rate_limits"] = True

        # Recompute derived fields here so ETAs stay fresh on every merge tick
        # rather than drifting from the per-machine fetch time.
        now_utc = datetime.now(timezone.utc)
        for prefix, total_secs in WINDOW_SECONDS.items():
            _enrich_window(out, prefix, total_secs, now_utc)

        # Extra-usage doesn't have a resets_at_iso (Anthropic's API doesn't
        # expose one for the monthly window) — derive pct_left only.
        try:
            extra_pct = int(out.get("extra_pct", "0") or "0")
        except ValueError:
            extra_pct = 0
        out["extra_pct_left"] = str(max(0, 100 - extra_pct))
    else:
        out["has_rate_limits"] = False

    # Rolling 30-day sums across machines. last_30d_cost_raw and _tokens_raw
    # are the canonical numbers; reformat the display strings from those so
    # rounding stays consistent regardless of how individual machines formatted
    # their per-machine partial sums.
    last_30d_cost = sum(
        float(p.get("last_30d_cost_raw", "0") or "0") for p in payloads
    )
    last_30d_tokens = sum(
        int(float(p.get("last_30d_tokens_raw", "0") or "0")) for p in payloads
    )
    last_30d_msgs = sum(parse_int(p.get("last_30d_msgs", "0")) for p in payloads)
    # active days is per-machine; combining requires per-day sets which the
    # source gists don't carry. Take max as a reasonable approximation: a busy
    # cross-machine day still only counts once, but this avoids double-counting
    # the same day across machines.
    last_30d_active_days = max(
        (parse_int(p.get("last_30d_active_days", "0")) for p in payloads), default=0
    )
    out["last_30d_cost"] = fmt_currency(last_30d_cost)
    out["last_30d_tokens"] = fmt_tokens(last_30d_tokens)
    out["last_30d_msgs"] = fmt_int(last_30d_msgs)
    out["last_30d_active_days"] = fmt_int(last_30d_active_days)
    out["last_30d_avg"] = fmt_currency(last_30d_cost / 30.0 if last_30d_cost else 0.0)
    out["last_30d_proj"] = fmt_currency(last_30d_cost)  # next 30 ≈ past 30 at current pace

    # Newest updated_at_iso for the title bar's "Updated Xm ago".
    out["updated_at_iso"] = max(
        (str(p.get("updated_at_iso", "")) for p in payloads if p.get("updated_at_iso")),
        default="",
    )

    for k in CURRENCY_SUMS:
        total = sum(parse_currency(p.get(k, "0")) for p in payloads)
        out[k] = fmt_currency(total)

    for k in TOKEN_SUMS:
        total = sum(parse_tokens(p.get(k, "0")) for p in payloads)
        out[k] = fmt_tokens(total)

    for k in INT_SUMS:
        total = sum(parse_int(p.get(k, "0")) for p in payloads)
        out[k] = fmt_int(total)

    for k in INT_MAXES:
        out[k] = fmt_int(max((parse_int(p.get(k, "0")) for p in payloads), default=0))

    # Hours today: SUM minutes across machines, then format. session_stats
    # emits "21m" for minutes and "X.Yh" once it crosses an hour. parse_currency
    # would interpret a lowercase "m" as million; that bug surfaced as
    # "21000000.0h coded" on the device. Parse explicitly here.
    out["hours_today"] = _fmt_hours_minutes(
        sum(_parse_hours_minutes(p.get("hours_today", "0")) for p in payloads)
    )

    # Cache pct / cost-per-req / daily avg recomputed from raw sums.
    today_cache_total = parse_tokens(out["today_cache_read"]) + parse_tokens(
        out["today_cache_write"]
    )
    today_total_tokens = parse_tokens(out["today_tokens"])
    out["cache_pct"] = (
        fmt_int(round(today_cache_total / today_total_tokens * 100))
        if today_total_tokens
        else "0"
    )

    today_cost = parse_currency(out["today_cost"])
    today_requests = parse_int(out["today_requests"])
    out["cost_per_req"] = (
        f"{today_cost / today_requests:.2f}" if today_requests else "0.00"
    )

    week_cost = parse_currency(out["week_cost"])
    out["daily_avg"] = f"{week_cost / 7:.2f}" if week_cost else "0.00"

    all_sessions = parse_int(out["all_sessions"])
    all_time_cost = parse_currency(out["all_time_cost"])
    out["avg_session_cost"] = (
        f"{all_time_cost / all_sessions:.2f}" if all_sessions else "0.00"
    )

    # Cost trend: recompute from week sum vs prior-week sum if both are present,
    # else inherit from the highest-cost machine to keep something useful.
    out["cost_trend"] = max(
        (str(p.get("cost_trend", "—")) for p in payloads),
        key=lambda v: parse_currency(v.lstrip("+-%")),
        default="—",
    )

    # Primary model: choose by total tokens summed across payloads.
    model_totals = {"opus": 0, "sonnet": 0, "haiku": 0}
    for m in model_totals:
        model_totals[m] = sum(parse_tokens(p.get(f"{m}_tokens", "0")) for p in payloads)
    grand = sum(model_totals.values()) or 1
    primary = max(model_totals, key=model_totals.get)
    out["primary_model"] = primary
    out["primary_pct"] = fmt_int(round(model_totals[primary] / grand * 100))
    for m, total in model_totals.items():
        out[f"{m}_pct"] = fmt_int(round(total / grand * 100))
    out["model_line"] = f"{primary}:{sum(parse_int(p.get('today_msgs', '0')) for p in payloads)}"

    # Set-union concat, deduped, "—"-stripped.
    for k in ("mcp_names", "top_project"):
        names: list[str] = []
        for p in payloads:
            for fragment in str(p.get(k, "")).split(","):
                f = fragment.strip()
                if f and f not in ("—", "none") and f not in names:
                    names.append(f)
        out[k] = ", ".join(names) if names else "none"

    # Day buckets pass through max cost / sum activity from latest machine.
    for d in DAY_BUCKETS:
        # Labels are deterministic (day-of-week), use whichever payload has them.
        out[f"{d}_lbl"] = next(
            (str(p.get(f"{d}_lbl", "")) for p in payloads if p.get(f"{d}_lbl")),
            "",
        )
        out[f"{d}_cost"] = fmt_currency(
            sum(parse_currency(p.get(f"{d}_cost", "0")) for p in payloads)
        )
        # Pct shown per-day in some layouts; recompute as bucket / max bucket.
    bucket_costs = [parse_currency(out[f"{d}_cost"]) for d in DAY_BUCKETS]
    peak = max(bucket_costs) or 1
    for d, cost in zip(DAY_BUCKETS, bucket_costs):
        out[f"{d}_pct"] = fmt_int(round(cost / peak * 100))

    # Newest updated_at.
    out["updated_at"] = max(
        (str(p.get("updated_at", "")) for p in payloads),
        default=datetime.now().strftime("%b %d at %I:%M%p"),
    )
    out["machines"] = len(payloads)

    return out


# ---------------------------------------------------------------------------
# Gist write.
# ---------------------------------------------------------------------------


def write_file(merged: dict, output_path: str) -> None:
    """Write the merged payload to a local file the reverse proxy serves.

    Caddy on the Hetzner host is configured (via the trmnl fork's compose +
    Caddyfile) to bind-mount /opt/terminus-dashboard read-only and serve
    /dashboard/* with no-cache headers. The Terminus poll extension fetches
    that URL — predictable freshness, no GitHub raw-gist CDN cache to fight.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX — no half-written reads from Caddy


def write_gist(merged: dict, gist_id: str, filename: str, token: str) -> None:
    body = json.dumps(
        {"files": {filename: {"content": json.dumps(merged, indent=2)}}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "claude-dashboard-merger/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"gist patch failed {resp.status}: {resp.read()!r}")


def main() -> None:
    cfg = load_config()
    sources = [u.strip() for u in cfg["SOURCE_URLS"].split(",") if u.strip()]
    payloads: list[dict] = []
    for url in sources:
        try:
            payloads.append(fetch(url))
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            sys.stderr.write(f"warn: failed to fetch {url}: {e}\n")

    if not payloads:
        sys.exit("no source payloads fetched")

    merged = merge(payloads)
    output_path = cfg["MERGED_OUTPUT_PATH"]
    write_file(merged, output_path)
    size = len(json.dumps(merged))

    # Optional: also write the gist for backup / external diagnostic. Skipped
    # silently if either MERGED_GIST_ID or GH_TOKEN is missing so a host that
    # only serves via Caddy doesn't need the PAT.
    if cfg.get("MERGED_GIST_ID") and cfg.get("GH_TOKEN"):
        try:
            write_gist(
                merged,
                cfg["MERGED_GIST_ID"],
                cfg["MERGED_GIST_FILENAME"],
                cfg["GH_TOKEN"],
            )
        except Exception as e:  # noqa: BLE001 — gist write is best-effort
            sys.stderr.write(f"warn: gist write failed (file write succeeded): {e}\n")

    sys.stdout.write(
        f"merged {len(payloads)} sources → {output_path} ({size} bytes)\n"
    )


if __name__ == "__main__":
    main()
