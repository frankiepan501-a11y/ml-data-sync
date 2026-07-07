param(
  [string]$N8nBaseUrl = $env:N8N_BASE_URL,
  [string]$N8nApiKey = $env:N8N_API_KEY,
  [string]$MlSyncToken = $env:ML_SYNC_SERVICE_AUTH_TOKEN
)

$ErrorActionPreference = "Stop"

if (-not $N8nBaseUrl) { throw "N8N_BASE_URL is required" }
if (-not $N8nApiKey) { throw "N8N_API_KEY is required" }
if (-not $MlSyncToken) { throw "ML_SYNC_SERVICE_AUTH_TOKEN is required" }

$env:N8N_BASE_URL = $N8nBaseUrl
$env:N8N_API_KEY = $N8nApiKey
$env:ML_SYNC_SERVICE_AUTH_TOKEN = $MlSyncToken

$script = Join-Path $PSScriptRoot "update_ml_close_workflows.py"
$python = $null
foreach ($candidate in @("python", "py", "C:\tmp\py311-embed\python.exe")) {
  try {
    $cmd = Get-Command $candidate -ErrorAction Stop
    $python = $cmd.Source
    break
  } catch {}
}
if (-not $python) { throw "No Python runtime found for $script" }

& $python $script
if ($LASTEXITCODE -ne 0) { throw "Python migration failed with exit code $LASTEXITCODE" }
