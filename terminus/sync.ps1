# Wrapper invoked by Windows Task Scheduler every 15 minutes.
# Loads the gist ID from .gist_id (gitignored, written manually or via setup),
# sets PATH, and runs sync_to_gist.py.

$gistFile = Join-Path $PSScriptRoot ".gist_id"
if (-not (Test-Path $gistFile)) {
    Write-Error "missing .gist_id at $gistFile (one line, the gist ID to push to)"
    exit 2
}
$env:CLAUDE_DASHBOARD_GIST_ID = (Get-Content $gistFile -Raw).Trim()
if (-not $env:CLAUDE_DASHBOARD_GIST_ID) {
    Write-Error ".gist_id is empty"
    exit 2
}

$env:Path = "C:\Program Files\GitHub CLI;" + $env:Path

$logDir = Join-Path $env:LOCALAPPDATA "claude-dashboard-trmnl"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "sync.log"

$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"
"[$timestamp] starting" | Out-File -FilePath $logFile -Append -Encoding utf8
& py "$PSScriptRoot\sync_to_gist.py" 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
"[$timestamp] exit=$LASTEXITCODE" | Out-File -FilePath $logFile -Append -Encoding utf8
exit $LASTEXITCODE
