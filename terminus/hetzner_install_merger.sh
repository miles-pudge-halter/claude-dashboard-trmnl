#!/usr/bin/env bash
# One-time install of the dashboard merger on the Hetzner box.
# Idempotent — safe to re-run after edits.
#
# Run on the Hetzner host (already SSH'd in as root):
#
#   bash hetzner_install_merger.sh
#
# Requires /etc/terminus-dashboard.env to exist with the right keys (the script
# refuses to install otherwise; it will template a stub if missing).

set -euo pipefail

INSTALL_DIR="/opt/terminus-dashboard"
ENV_PATH="/etc/terminus-dashboard.env"
LOG_PATH="/var/log/terminus-dashboard.log"
CRON_PATH="/etc/cron.d/terminus-dashboard"

say() { printf "==> %s\n" "$*"; }

say "ensuring install dir"
mkdir -p "${INSTALL_DIR}"
chmod 755 "${INSTALL_DIR}"

if [[ ! -f "${INSTALL_DIR}/merger.py" ]]; then
  say "merger.py not present at ${INSTALL_DIR}/merger.py — copy it manually before continuing"
  exit 2
fi
chmod 755 "${INSTALL_DIR}/merger.py"

if [[ ! -f "${ENV_PATH}" ]]; then
  say "templating env file at ${ENV_PATH} (you must edit before the cron will succeed)"
  cat > "${ENV_PATH}" <<'TEMPLATE'
# Comma-separated raw gist URLs to merge from.
SOURCE_URLS=https://gist.githubusercontent.com/USER/WIN_GIST_ID/raw/data.json,https://gist.githubusercontent.com/USER/MAC_GIST_ID/raw/data.json

# The gist ID to write the merged payload into. Owned by miles-pudge-halter so the GH_TOKEN below can write it.
MERGED_GIST_ID=

# Filename within the merged gist.
MERGED_GIST_FILENAME=data.json

# Personal access token, gist scope only. Generate at https://github.com/settings/tokens
GH_TOKEN=
TEMPLATE
  chown root:root "${ENV_PATH}"
  chmod 600 "${ENV_PATH}"
  say "edit ${ENV_PATH} to fill in real values, then re-run this script"
  exit 3
fi

if grep -qE "^(SOURCE_URLS|MERGED_GIST_ID|GH_TOKEN)=$" "${ENV_PATH}" || grep -q "WIN_GIST_ID" "${ENV_PATH}"; then
  say "${ENV_PATH} still has placeholder values — fill in real values before re-running"
  exit 4
fi

touch "${LOG_PATH}"
chown root:root "${LOG_PATH}"
chmod 640 "${LOG_PATH}"

say "writing cron entry to ${CRON_PATH}"
cat > "${CRON_PATH}" <<'CRON'
# Merge per-machine claude-code-dashboard gists into a single merged gist.
# Runs every 15 minutes at +5,20,35,50 — offset 5 minutes after the per-machine
# pushes so both have updated by then.

5,20,35,50 * * * * root /usr/bin/python3 /opt/terminus-dashboard/merger.py >> /var/log/terminus-dashboard.log 2>&1
CRON
chmod 644 "${CRON_PATH}"

say "running merger once now to validate config"
if /usr/bin/python3 "${INSTALL_DIR}/merger.py"; then
  say "ok — cron will run every 15 minutes; tail -f ${LOG_PATH}"
else
  say "merger run failed — fix the issue, then re-run this script"
  exit 5
fi
