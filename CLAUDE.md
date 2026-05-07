# claude-dashboard-trmnl — Terminus BYOS adapter

This repo is a public fork of
[`rohitg00/claude-dashboard-trmnl`](https://github.com/rohitg00/claude-dashboard-trmnl).
Upstream targets TRMNL Cloud's Private Plugin webhook flow; this fork
adds a `terminus/` subdirectory that adapts the same data shape to a
self-hosted Terminus server (the [`miles-pudge-halter/terminus` fork](https://github.com/miles-pudge-halter/terminus),
deployed at `https://terminus.ayoe.me`). All adapter code lives under
`terminus/` — the rest of the tree is upstream and gets occasional
`git pull` for cherry-pickable improvements.

## Architecture

```
[ Mac ]                       [ Windows ]
launchd, every 15 min          Task Scheduler, every 15 min
  → sync.ps1 / sync_to_gist.py    (same wrapper, OS-aware)
       → claude_oauth_usage.py    rate-limit fields from Anthropic OAuth API
       → scanner.py               cost + tokens, codex-bar pricing + dedup
       → gh gist edit             push JSON to per-machine secret gist

                          ↓
[ Hetzner box ] cron, every 15 min at +5,20,35,50
  /opt/terminus-dashboard/merger.py
       → fetches both per-machine gists
       → recomputes derived rate-limit fields (eta, pace, reserve)
       → sums cross-machine cost / tokens / sessions
       → writes /opt/terminus-dashboard/merged.json (atomic)

                          ↓
[ Caddy on terminus.ayoe.me ]
  https://terminus.ayoe.me/dashboard/merged.json
       (Cache-Control: no-cache, no-store, must-revalidate)

                          ↓
[ Terminus Poll extension ]
  every 15 min → Liquid template renders → device
```

The Caddy hop replaces an earlier "merger writes a third gist that
Terminus polls" design. GitHub's raw-gist CDN ignores
`Cache-Control: no-cache` on read and serves stale content for ~5–15
minutes after a write — so the merger output goes through Caddy where
we control caching, while the per-machine gists stay on GitHub
(machine-private storage with no shared TTL concerns).

## What's in `terminus/`

| File | Runs on | Purpose |
|---|---|---|
| `sync_to_gist.py` | Mac + Windows | Top-level wrapper; calls scanner, OAuth fetch, formats payload, pushes to per-machine gist |
| `scanner.py` | Mac + Windows | Walks `~/.claude/projects/**/*.jsonl` and `~/.config/claude/projects/**/*.jsonl`. Replaces upstream's `session_stats.gather_stats()` with a port of codex bar's logic — streaming-chunk dedup by `(messageId, requestId)`, codex bar's pricing tables, no fast-mode 6× multiplier, sonnet 200K-token threshold tier. Output keys mirror upstream so the rest of the pipeline is unchanged. |
| `claude_cost.py` | Mac + Windows | Pricing constants + `claude_cost_usd()` ported from `steipete/codexbar`'s `CostUsagePricing.swift`. Threshold-aware tiering. |
| `claude_oauth_usage.py` | Mac + Windows | Calls `https://api.anthropic.com/api/oauth/usage` with the Claude CLI's OAuth token (resolved from macOS Keychain entry `Claude Code-credentials`, then `~/.claude/.credentials.json`, then `CLAUDE_OAUTH_TOKEN` env). Maps Anthropic's `{five_hour, seven_day, seven_day_sonnet, extra_usage}` blocks. Token must have `user:profile` scope; CLI-only `user:inference` returns 403. |
| `sync.ps1` | Windows | Task Scheduler launcher; sets `CLAUDE_DASHBOARD_GIST_ID` from `terminus/.gist_id`, runs `sync_to_gist.py`, logs to `%LOCALAPPDATA%\claude-dashboard-trmnl\sync.log`. |
| `mac_setup.sh` | Mac | Idempotent installer: provisions `terminus/.venv` with `pexpect`+`pyte` (kept as scraper fallback), creates a per-machine secret gist via `gh`, writes the launchd plist and loads it. Run once; safe to re-run. |
| `merger.py` | Hetzner | Fetches per-machine gists, recomputes rate-limit derived fields at merge time so ETAs stay fresh, sums numerics, atomically writes `/opt/terminus-dashboard/merged.json`. Optional best-effort gist write if `MERGED_GIST_ID` + `GH_TOKEN` are still in `/etc/terminus-dashboard.env`. |
| `hetzner_install_merger.sh` | Hetzner | Idempotent installer for the cron entry + env file template. |
| `template_full.liquid` | Terminus admin UI | The Liquid template pasted into the Poll extension. References `source_1.X` because Terminus wraps polled JSON under `source_1`. Wraps content in `<div class="{{extension.css_classes}}"><div class="view view--full">` — required for the framework's CSS variables (progress bar fill colors, etc.) to bind. |

## Why three machines all run the same `sync_to_gist.py`

Each developer machine has its own `~/.claude/projects/`. The OAuth
endpoint is per-account — same numbers regardless of which machine
queries it, but only one is needed; the merger picks the freshest
source. Per-machine cost/tokens come from the local JSONL scan and
get summed across machines on the merger.

## Local config (per-machine, gitignored)

- `terminus/.gist_id` — one-line gist ID. Created by `mac_setup.sh` on
  Mac. On Windows it's set manually after running `gh gist create
  data.json`.
- `terminus/.venv/` — virtualenv with `pexpect` + `pyte` for the
  upstream TUI scraper fallback. Mac only; the OAuth path doesn't need
  it but `mac_setup.sh` provisions it for resilience.

## Pulling upstream improvements

```
git fetch upstream
git rebase upstream/main      # in case upstream session_stats fixes
                              # are wanted; we don't depend on its
                              # gather_stats() anymore but its plugin
                              # and MCP detection helpers are still
                              # called.
```

The fork is public; everything in `terminus/` is checked in except
secrets (gist IDs, the venv, logs).
