# Claude Code Dashboard → Terminus (BYOS) adapter

This subdir adapts [`rohitg00/claude-dashboard-trmnl`](https://github.com/rohitg00/claude-dashboard-trmnl) — built for TRMNL Cloud's webhook plugin model — to a self-hosted Terminus server. Terminus has no webhook ingest, so each developer machine pushes a per-machine JSON to a private GitHub gist, an always-on host (Hetzner) merges them and serves the result via Caddy, and a Terminus Poll extension fetches it on a schedule.

```
[ Mac ]                       [ Windows ]
launchd, every 15 min          Task Scheduler, every 15 min
  → sync_to_gist.py wrapper      same
       → claude_oauth_usage.py    rate-limit fields from Anthropic OAuth API
       → scanner.py               cost + tokens (codex-bar pricing + dedup)
       → gh gist edit             push JSON to per-machine secret gist

                          ↓
[ Hetzner box ] cron, every 15 min at +5,20,35,50
  /opt/terminus-dashboard/merger.py
       → fetch both per-machine gists
       → recompute rate-limit derived fields (eta, pace, reserve)
       → sum cost / tokens / sessions
       → atomic write /opt/terminus-dashboard/merged.json

                          ↓
[ Caddy on terminus.ayoe.me ]
  https://terminus.ayoe.me/dashboard/merged.json
       Cache-Control: no-cache, no-store, must-revalidate

                          ↓
[ Terminus Poll extension ]
  every 15 min → Liquid template renders → device
```

## Files in this directory

| File | Runs on | What it does |
|---|---|---|
| `sync_to_gist.py` | Mac + Windows | Top-level wrapper. Calls `scanner.gather_stats()`, `claude_oauth_usage.maybe_run_scraper()`, formats the payload, pushes via `gh gist edit`. |
| `scanner.py` | Mac + Windows | Walks `~/.claude/projects/**/*.jsonl` (and `~/.config/claude/projects/**/*.jsonl`). Replaces upstream `session_stats.gather_stats()` with a port of codex bar's parsing — see "Cost calculation" below. |
| `claude_cost.py` | Mac + Windows | Pricing tables + `claude_cost_usd()`, ported from `steipete/codexbar`. |
| `claude_oauth_usage.py` | Mac + Windows | Calls Anthropic's OAuth usage API for session/weekly/extra rate-limit blocks. |
| `sync.ps1` | Windows | Task Scheduler launcher. Reads gist ID from `terminus/.gist_id`. Logs to `%LOCALAPPDATA%\claude-dashboard-trmnl\sync.log`. |
| `mac_setup.sh` | Mac | Idempotent installer — `gh` auth check, venv with pexpect/pyte (scraper fallback), creates a per-machine gist, writes + loads the launchd plist. |
| `merger.py` | Hetzner | Fetches per-machine gists, recomputes derived fields, atomic-writes `/opt/terminus-dashboard/merged.json`. Optional best-effort gist write for backup. |
| `hetzner_install_merger.sh` | Hetzner | One-time merger installer (cron entry, env file template). |
| `template_full.liquid` | Terminus admin UI | Liquid template pasted into the Poll extension's Template field. |

## Cost calculation

`scanner.py` and `claude_cost.py` together replace upstream `session_stats`'s pricing path with a port of `steipete/codexbar`'s logic. Three meaningful differences:

1. **Streaming-chunk dedup.** Claude Code's JSONL writes multiple lines per assistant message during streaming, each with **cumulative** token counts. session_stats sums every line — over-counts tokens and cost by ~3–5×. Scanner keys rows by `(messageId, requestId)` and keeps only the last cumulative state per message.
2. **No fast-mode 6× multiplier.** session_stats has a separate `opus_fast` pricing tier triggered by `usage.speed == "fast"` for opus-4-6 messages. Codex bar uses the same per-token rate regardless of speed.
3. **Sonnet 4.5 / 4.6 200K threshold tier.** Above 200K tokens these models charge 2× per token. session_stats treats sonnet flat.

Plugin and MCP detection still delegates to `session_stats._plugins()` / `_mcps()` — those don't depend on pricing.

## Per-machine setup

### Mac

```bash
git clone https://github.com/miles-pudge-halter/claude-dashboard-trmnl.git ~/Projects/claude-dashboard-trmnl
cd ~/Projects/claude-dashboard-trmnl/terminus
chmod +x mac_setup.sh
./mac_setup.sh
```

`mac_setup.sh` will:
- Verify `gh` and `python3` are present.
- Provision `terminus/.venv` with `pexpect` + `pyte`.
- Run a dry validation of `sync_to_gist.py` (no gist push).
- Create a new secret gist on whatever account `gh` is authenticated as. Records the ID in `terminus/.gist_id` (gitignored).
- Write `~/Library/LaunchAgents/me.ayoe.claude-dashboard-sync.plist` with `StartInterval=900` and load it.

When the script finishes it prints the gist ID + raw URL. Send the gist ID to whoever runs the merger so it can be added to `/etc/terminus-dashboard.env` on the Hetzner box.

### Windows

The Windows side runs via Task Scheduler. The setup is mostly manual — there's no equivalent `windows_setup.ps1` in the repo (yet):

1. `gh auth login` (browser flow).
2. Generate a new secret gist:
   ```powershell
   $tmp = New-Item -ItemType Directory "$env:TEMP\cdash" -Force
   "{}" | Out-File "$tmp\data.json" -Encoding utf8 -NoNewline
   gh gist create "$tmp\data.json" --desc "claude code dashboard data — windows"
   ```
   The output prints `https://gist.github.com/USER/<ID>` — copy that ID.
3. Save it:
   ```powershell
   "<ID>" | Out-File -FilePath "$env:USERPROFILE\Projects\claude-dashboard-trmnl\terminus\.gist_id" -Encoding ASCII -NoNewline
   ```
4. Schedule:
   ```powershell
   $tn = "Claude Code Dashboard Sync"
   $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\ayoe\Projects\claude-dashboard-trmnl\terminus\sync.ps1"'
   schtasks /Create /TN $tn /SC MINUTE /MO 15 /TR $cmd /F /RU $env:USERNAME /IT
   schtasks /Run /TN $tn          # one immediate run to verify
   ```
5. Tail logs:
   ```powershell
   Get-Content "$env:LOCALAPPDATA\claude-dashboard-trmnl\sync.log" -Tail 10 -Wait
   ```

## Hetzner merger setup

```bash
ssh root@terminus.ayoe.me
mkdir -p /opt/terminus-dashboard
# upload merger.py and hetzner_install_merger.sh into /opt/terminus-dashboard/
chmod 755 /opt/terminus-dashboard/{merger.py,install.sh}
nano /etc/terminus-dashboard.env       # fill in SOURCE_URLS (raw gist URLs, comma-separated)
bash /opt/terminus-dashboard/install.sh
```

Required env keys:

```
SOURCE_URLS=https://gist.githubusercontent.com/.../raw/data.json,https://gist.githubusercontent.com/.../raw/data.json
MERGED_OUTPUT_PATH=/opt/terminus-dashboard/merged.json   # default
# Optional — write a backup gist:
MERGED_GIST_ID=<id>
MERGED_GIST_FILENAME=data.json
GH_TOKEN=ghp_xxx                                          # gist scope only
```

The Caddy service in the [trmnl fork](https://github.com/miles-pudge-halter/terminus) bind-mounts `/opt/terminus-dashboard` read-only into the Caddy container; the file is served at `https://terminus.ayoe.me/dashboard/merged.json` with `Cache-Control: no-cache` headers.

## Terminus extension setup (one-time)

1. Open https://terminus.ayoe.me, sign in.
2. **Extensions → New**:
   - **Label**: `Claude Code Dashboard`
   - **Name**: `claude_code_dashboard`
   - **Kind**: **Poll**
   - **Verb**: `GET`
   - **URLs** (one per line):
     ```
     https://terminus.ayoe.me/dashboard/merged.json
     ```
   - **Headers**: leave empty. Caddy serves `application/json` with no-cache, so the historical `Cache-Control` and `Accept` workarounds are unnecessary.
   - **Body**: empty.
   - **Template**: paste the entire contents of `template_full.liquid`.
   - **Models**: attach the device model — without one, Build silently no-ops because the upstream extension build job iterates `extension.models.each`.
   - **Polling interval**: 15 minutes (matches the per-machine sync cadence; faster wastes cycles since the gist won't be newer).
3. Save. Click **Build** (gear-and-hammer icon, top-right of the edit page) to produce a Screen.
4. **Playlists → edit → New item** → select that screen.

Re-clicking Build (manually or from the extension's polling schedule) updates the same Screen idempotently via `ScreenUpserter`.

## Pulling upstream improvements

This adapter sits in a `terminus/` subdir of the fork's clone. Upstream changes pull in cleanly because nothing under `terminus/` is upstream-tracked:

```
cd ~/Projects/claude-dashboard-trmnl
git pull
```

If upstream `session_stats.py` adds a useful field (e.g., a new metric), call it from `scanner.py`'s `gather_stats()` rather than reverting to `session_stats.gather_stats()` — we deliberately diverge on the cost path.

## Operations

| Action | Command |
|---|---|
| Force a Mac sync now | `launchctl start me.ayoe.claude-dashboard-sync` |
| Force a Windows sync now | `schtasks /Run /TN "Claude Code Dashboard Sync"` |
| Force the merger to run | `ssh root@terminus.ayoe.me '/usr/bin/python3 /opt/terminus-dashboard/merger.py'` |
| Tail merger logs | `ssh root@terminus.ayoe.me 'tail -f /var/log/terminus-dashboard.log'` |
| Inspect served JSON | `curl -s https://terminus.ayoe.me/dashboard/merged.json \| jq` |

## Known limitations

- **OAuth token scope.** The usage endpoint requires `user:profile`. CLI-only tokens with `user:inference` return 403 and the wrapper falls back to zeroed rate-limit fields (visible as `has_rate_limits: false` in the gist).
- **Cross-machine streak.** `streak` is `max()` across machines — slightly inaccurate when activity spans different days on different machines (true streak is "consecutive days with activity anywhere", which would need per-day activity sets in the source gists). Acceptable for typical usage.
- **Currency display.** The OAuth API returns `currency` (e.g. `EUR`) but the template hardcodes `$`. Cosmetic; the value field carries the right amount.
