#!/usr/bin/env bash
# One-time setup for the Claude Code Dashboard sync on macOS.
# Mirror of the Windows Task Scheduler path, but using launchd for scheduling.
#
# What this does:
#   1. Verifies prerequisites (gh CLI, python3).
#   2. Clones rohitg00/claude-dashboard-trmnl into ~/Projects/ if absent.
#   3. Drops the terminus/ adapter dir into the clone.
#   4. Creates a *new* secret gist on whatever GitHub account `gh` is currently
#      authenticated as.
#   5. Writes a launchd plist that runs sync_to_gist.py every 15 minutes and
#      loads it.
#
# Re-runnable: detects existing state and skips already-completed steps.

set -euo pipefail

BASE_DIR="${HOME}/Projects/claude-dashboard-trmnl"
TERMINUS_DIR="${BASE_DIR}/terminus"
PLIST_LABEL="me.ayoe.claude-dashboard-sync"
PLIST_PATH="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/claude-dashboard-trmnl"

say() { printf "\033[36m==>\033[0m %s\n" "$*"; }
fail() { printf "\033[31merror:\033[0m %s\n" "$*" >&2; exit 1; }

say "checking prerequisites"
command -v gh       >/dev/null || fail "gh CLI not installed (brew install gh)"
command -v python3  >/dev/null || fail "python3 not installed (brew install python)"
gh auth status >/dev/null 2>&1 || fail "gh not authenticated; run: gh auth login"

GH_USER=$(gh api user --jq .login)
say "gh authenticated as: ${GH_USER}"

if [[ ! -d "${BASE_DIR}/.git" ]]; then
  say "cloning rohitg00/claude-dashboard-trmnl into ${BASE_DIR}"
  mkdir -p "$(dirname "${BASE_DIR}")"
  git clone https://github.com/rohitg00/claude-dashboard-trmnl.git "${BASE_DIR}"
else
  say "repo already cloned at ${BASE_DIR}"
fi

if [[ ! -f "${TERMINUS_DIR}/sync_to_gist.py" ]]; then
  fail "terminus/ adapter not found in ${TERMINUS_DIR}.
Copy sync_to_gist.py and template_full.liquid from the Windows box into ${TERMINUS_DIR}/, then re-run this script."
fi

say "validating sync_to_gist.py runs locally (dry — no gist push)"
CLAUDE_DASHBOARD_GIST_ID="" python3 -c "
import sys
sys.path.insert(0, '${TERMINUS_DIR}')
from sync_to_gist import merge_payload
import json
d = merge_payload()
print('today_cost:', d['today_cost'], 'plugin_count:', d['plugin_count'], 'mcp_count:', d['mcp_count'])
" || fail "sync_to_gist.py failed; check ~/.claude/projects/ exists and has session jsonls"

if [[ -f "${TERMINUS_DIR}/.gist_id" ]]; then
  GIST_ID=$(cat "${TERMINUS_DIR}/.gist_id")
  say "reusing existing gist id from .gist_id: ${GIST_ID}"
else
  say "creating a new secret gist on ${GH_USER}"
  TMP=$(mktemp -d)
  python3 -c "
import sys, json
sys.path.insert(0, '${TERMINUS_DIR}')
from sync_to_gist import merge_payload
print(json.dumps(merge_payload(), indent=2))
" > "${TMP}/data.json"
  GIST_URL=$(gh gist create "${TMP}/data.json" --desc "claude code dashboard data — mac" 2>&1 | tail -1)
  GIST_ID=$(basename "${GIST_URL}")
  echo "${GIST_ID}" > "${TERMINUS_DIR}/.gist_id"
  rm -rf "${TMP}"
  say "created gist: https://gist.github.com/${GH_USER}/${GIST_ID}"
fi

RAW_URL="https://gist.githubusercontent.com/${GH_USER}/${GIST_ID}/raw/data.json"
say "raw url: ${RAW_URL}"

mkdir -p "${LOG_DIR}"

say "writing launchd plist to ${PLIST_PATH}"
GH_BIN=$(command -v gh)
PY_BIN=$(command -v python3)
cat > "${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY_BIN}</string>
    <string>${TERMINUS_DIR}/sync_to_gist.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CLAUDE_DASHBOARD_GIST_ID</key>
    <string>${GIST_ID}</string>
    <key>GH_BIN</key>
    <string>${GH_BIN}</string>
    <key>PATH</key>
    <string>$(dirname "${GH_BIN}"):/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key>
  <integer>900</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/sync.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/sync.err</string>
</dict>
</plist>
PLIST

say "loading launchd job"
launchctl unload "${PLIST_PATH}" 2>/dev/null || true
launchctl load "${PLIST_PATH}"

say "triggering an immediate run"
launchctl start "${PLIST_LABEL}" || true
sleep 2
say "log tail:"
tail -5 "${LOG_DIR}/sync.log" 2>/dev/null || echo "  (no log yet — wait a few seconds and check ${LOG_DIR}/sync.log)"

cat <<EOF

\033[32mSetup complete.\033[0m

Mac gist id: ${GIST_ID}
Raw URL:     ${RAW_URL}

Send the gist ID back to whoever is wiring up the merger on Hetzner so it can be added to /etc/terminus-dashboard.env.

Manage:
  launchctl list | grep claude-dashboard       # see the job
  launchctl start ${PLIST_LABEL}                # run on demand
  tail -f ${LOG_DIR}/sync.log                   # watch logs
  launchctl unload ${PLIST_PATH}                # disable
EOF
