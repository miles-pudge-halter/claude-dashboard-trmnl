# Claude Code Dashboard → Terminus (BYOS) adapter

This subdir adapts [rohitg00/claude-dashboard-trmnl](https://github.com/rohitg00/claude-dashboard-trmnl) — built for TRMNL Cloud's webhook plugin model — to a self-hosted Terminus server (https://terminus.ayoe.me). Terminus has no webhook ingest, so we bridge by writing dashboard JSON to a private GitHub gist that a Terminus Poll extension fetches every 15 minutes.

```
[this Windows box]
  Task Scheduler (every 15 min)
    → sync.ps1
       → sync_to_gist.py
          → session_stats.gather_stats() (parses ~/.claude/projects/*.jsonl)
          → gh gist edit <ID> --filename data.json

[gist raw URL]
  https://gist.githubusercontent.com/miles-pudge-halter/<GIST_ID>/raw/data.json

[terminus.ayoe.me]
  Poll extension fetches the gist
    → Liquid template renders dashboard
    → Build action upserts a Screen
    → Screen lives in a Playlist
    → Playlist drives the device
```

## What's in this directory

- `sync_to_gist.py` — wrapper that imports the upstream `session_stats.gather_stats()`, patches the macOS-only desktop config path for Windows/Linux, adds zeroed placeholders for the rate-limit fields normally produced by `claude_usage_scraper.py` (which uses macOS-only pexpect), and pushes the merged JSON to the gist via `gh`.
- `sync.ps1` — Task Scheduler launcher. Sets `CLAUDE_DASHBOARD_GIST_ID`, prepends GitHub CLI to PATH, runs the Python wrapper, logs to `%LOCALAPPDATA%\claude-dashboard-trmnl\sync.log`.
- `template_full.liquid` — Terminus extension template. Adapted from the upstream `trmnl_plugin/markup_full.html`; all `{{ X }}` data references rewritten as `{{ source_1.X }}` (the Poll extension wraps fetched JSON under `source_1`). Rate-limit section is `{% if source_1.has_rate_limits %}`-gated and falls back to a today/week/month/all-time costs row on Windows where rate-limit data isn't available.

## What's deferred

`claude_usage_scraper.py` is **not** ported to Windows. It uses pexpect+pyte to scrape the Claude Code TUI's `/usage` page, which doesn't run cleanly on Windows. Without it we lose: `extra_spent`, `extra_limit`, `extra_pct`, `session_pct`, `week_all_pct`, `week_sonnet_pct`, and their `_reset_short` siblings. Costs/tokens/sessions/streaks/projects/MCPs/plugins all still work — that's ~80% of the value.

If you ever want the rate-limit data on Windows, options are: (1) port the scraper to use `wexpect` or a Windows ConPTY library; (2) run the scraper on a small macOS/Linux box (e.g., a Raspberry Pi) and merge its JSON with this Windows JSON before pushing to the gist; (3) wait for Anthropic to ship a non-TUI usage API.

## State

- Gist ID lives in `terminus/.gist_id` (one line, gitignored). Each machine has its own. Set it manually or have `mac_setup.sh` create the gist for you on first run.
- Scheduled task: `Claude Code Dashboard Sync`, runs every 15 minutes when the user is logged on, view via `schtasks /Query /TN "Claude Code Dashboard Sync"` or Task Scheduler GUI
- Logs: `%LOCALAPPDATA%\claude-dashboard-trmnl\sync.log`
- Run the sync on demand: `schtasks /Run /TN "Claude Code Dashboard Sync"`

## Terminus extension setup (one-time)

1. Open https://terminus.ayoe.me, sign in.
2. **Extensions → New**. Settings:
   - **Label**: `Claude Code Dashboard`
   - **Name**: `claude_code_dashboard`
   - **Kind**: **Poll**
   - **Verb**: `GET`
   - **URLs** (one per line):
     ```
     https://gist.githubusercontent.com/USER/MERGED_GIST_ID/raw/data.json
     ```
   - **Headers** (one per line in the form `Header: value`):
     ```
     Cache-Control: no-cache
     Accept: application/json
     ```
     `Cache-Control: no-cache` forces the gist CDN to serve fresh content on every poll. Without it you can see 5+ minute lag.
     `Accept: application/json` is **required** — GitHub's raw-gist endpoint serves `Content-Type: text/plain` regardless of file extension (XSS hardening), and Terminus's parser dispatches on MIME. With `text/plain` the body is `String#split`-ed into a whitespace-token array; with the `Accept` header Terminus's `Sole#maybe_alter_mime_type` overrides the parsing MIME and dispatches to JSON, giving you a proper hash at `source_1`.
   - **Body**: leave empty
   - **Template**: paste the entire contents of `template_full.liquid`
   - **Models**: attach the device model (the same one the device is provisioned to). **Without a model attached the Build button silently does nothing** — it iterates `extension.models.each` and zero models means zero screen jobs enqueued.
   - **Polling interval**: 15 minutes (matches the local sync cadence; faster wastes API quota)
3. Save.
4. From the extension's edit page click **Build** (gear-and-hammer icon next to Preview). It enqueues `Jobs::Batches::Extension` → `Jobs::Extensions::Screen` → fetch → render → screen upsert. ~30s.
5. Visit **Screens** — the rendered dashboard screen appears with the extension's label.
6. **Playlists → edit playlist → New item** → select the screen.
7. Re-clicking Build later (manual or via the polling schedule) updates the same screen idempotently via `ScreenUpserter`.

## Pulling upstream improvements

This adapter sits in a `terminus/` subdir of the upstream repo clone. To update the upstream pieces (`launchd-setup/session_stats.py` is the one we depend on):

```
cd C:\Users\ayoe\Projects\claude-dashboard-trmnl
git pull
```

The `terminus/` dir is untracked from the upstream's perspective so `git pull` is conflict-free.
