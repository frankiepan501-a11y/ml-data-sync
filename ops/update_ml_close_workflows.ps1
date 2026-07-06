param(
  [string]$N8nBaseUrl = $env:N8N_BASE_URL,
  [string]$N8nApiKey = $env:N8N_API_KEY,
  [string]$MlSyncToken = $env:ML_SYNC_SERVICE_AUTH_TOKEN
)

$ErrorActionPreference = "Stop"

if (-not $N8nBaseUrl) { throw "N8N_BASE_URL is required" }
if (-not $N8nApiKey) { throw "N8N_API_KEY is required" }
if (-not $MlSyncToken) { throw "ML_SYNC_SERVICE_AUTH_TOKEN is required" }

$Headers = @{ "X-N8N-API-KEY" = $N8nApiKey }
$MlAuth = "Bearer $MlSyncToken"

function Get-Workflow($id) {
  Invoke-RestMethod -Method Get -Uri "$N8nBaseUrl/workflows/$id" -Headers $Headers -TimeoutSec 90
}

function Update-Workflow($wf, $nodes, $connections) {
  $body = @{
    name = $wf.name
    nodes = $nodes
    connections = $connections
    settings = $wf.settings
  } | ConvertTo-Json -Depth 100
  Invoke-RestMethod -Method Put -Uri "$N8nBaseUrl/workflows/$($wf.id)" -Headers $Headers -ContentType "application/json" -Body $body -TimeoutSec 90 | Out-Null
  Write-Host "updated $($wf.id) $($wf.name)"
}

function AuthHeader() {
  @{ parameters = @(@{ name = "Authorization"; value = $MlAuth }) }
}

function ScheduleNode($node) { $node }

function HttpNode($id, $name, $x, $y, $url, $timeout = 180000, $auth = $true) {
  $headers = if ($auth) { AuthHeader } else { @{ parameters = @() } }
  @{
    id = $id
    name = $name
    type = "n8n-nodes-base.httpRequest"
    typeVersion = 4.2
    position = @($x, $y)
    parameters = @{
      method = "POST"
      url = $url
      sendHeaders = $auth
      headerParameters = $headers
      options = @{ timeout = $timeout }
    }
  }
}

function CodeNode($id, $name, $x, $y, $code) {
  @{
    id = $id
    name = $name
    type = "n8n-nodes-base.code"
    typeVersion = 2
    position = @($x, $y)
    parameters = @{ jsCode = $code }
  }
}

$periodCode = @'
const now = new Date(Date.now() + 8 * 3600 * 1000);
const d = new Date(now.getFullYear(), now.getMonth() - 1, 1);
const month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
const period = `month_${month}`;
return [{ json: { month, period } }];
'@

# 3号操作指引：固定发美客多群，由 ml-sync 使用聪哥3号发送交互卡。
$wf = Get-Workflow "ucq2vYbWVWiY98Fw"
$sched = ScheduleNode(($wf.nodes | Where-Object { $_.type -like "*.scheduleTrigger" } | Select-Object -First 1))
$nodes = @(
  $sched,
  (CodeNode "build-period" "Build period" 460 300 $periodCode),
  (HttpNode "send-instruction-card" "Send instruction card" 700 300 '=https://ml-sync.zeabur.app/report/ml-close/card?kind=instruction&period={{$json.period}}&send=true&receive_id=oc_cd007a8f1dbb4a78943625e5432a4cd7')
)
$connections = @{}
$connections[$sched.name] = @{ main = @(@(@{ node = "Build period"; type = "main"; index = 0 })) }
$connections["Build period"] = @{ main = @(@(@{ node = "Send instruction card"; type = "main"; index = 0 })) }
Update-Workflow $wf $nodes $connections

# CBT导出解析：ingest 成功后由 ml-sync 自动重算+审计，再发送下一张卡。
$wf = Get-Workflow "j5I4vcjwarGgols0"
$sched = ScheduleNode(($wf.nodes | Where-Object { $_.type -like "*.scheduleTrigger" } | Select-Object -First 1))
$ingest = HttpNode "cbt-ingest" "CBT ingest" 500 300 "https://ml-sync.zeabur.app/report/cbt-ingest?commit=true" 300000
$buildCard = CodeNode "build-next-card" "Build next card request" 740 300 @'
const j = $input.first().json;
const audit = j.post_ingest?.ml_close_audit || {};
const period = audit.period || (j.month ? `month_${j.month}` : '');
const kind = audit.next_card || 'cost_gap';
return [{ json: { period, kind, url: `https://ml-sync.zeabur.app/report/ml-close/card?period=${encodeURIComponent(period)}&kind=${encodeURIComponent(kind)}&send=true` } }];
'@
$sendCard = HttpNode "send-next-card" "Send next card" 980 300 '={{$json.url}}' 180000
$nodes = @($sched, $ingest, $buildCard, $sendCard)
$connections = @{}
$connections[$sched.name] = @{ main = @(@(@{ node = "CBT ingest"; type = "main"; index = 0 })) }
$connections["CBT ingest"] = @{ main = @(@(@{ node = "Build next card request"; type = "main"; index = 0 })) }
$connections["Build next card request"] = @{ main = @(@(@{ node = "Send next card"; type = "main"; index = 0 })) }
Update-Workflow $wf $nodes $connections

# 9号成本审计：重算成本、审计状态台、发送下一张交互卡。
$wf = Get-Workflow "CWnmOuOmrde5bIkG"
$sched = ScheduleNode(($wf.nodes | Where-Object { $_.type -like "*.scheduleTrigger" } | Select-Object -First 1))
$recalc = HttpNode "recalc-cost" "Recalc cost + audit" 700 300 '=https://ml-sync.zeabur.app/report/ml-close/recalc-cost?period={{$json.period}}&commit=true' 300000
$buildCard = CodeNode "build-cost-card" "Build cost card request" 940 300 @'
const j = $input.first().json;
const audit = j.audit || {};
const period = audit.period || j.period;
const kind = audit.next_card || 'cost_gap';
return [{ json: { period, kind, url: `https://ml-sync.zeabur.app/report/ml-close/card?period=${encodeURIComponent(period)}&kind=${encodeURIComponent(kind)}&send=true` } }];
'@
$sendCard = HttpNode "send-cost-card" "Send cost card" 1180 300 '={{$json.url}}' 180000
$nodes = @($sched, (CodeNode "build-period" "Build period" 460 300 $periodCode), $recalc, $buildCard, $sendCard)
$connections = @{}
$connections[$sched.name] = @{ main = @(@(@{ node = "Build period"; type = "main"; index = 0 })) }
$connections["Build period"] = @{ main = @(@(@{ node = "Recalc cost + audit"; type = "main"; index = 0 })) }
$connections["Recalc cost + audit"] = @{ main = @(@(@{ node = "Build cost card request"; type = "main"; index = 0 })) }
$connections["Build cost card request"] = @{ main = @(@(@{ node = "Send cost card"; type = "main"; index = 0 })) }
Update-Workflow $wf $nodes $connections

function Update-GatedWorkflow($id, $targetUrl) {
  $wf = Get-Workflow $id
  $sched = ScheduleNode(($wf.nodes | Where-Object { $_.type -like "*.scheduleTrigger" } | Select-Object -First 1))
  $status = HttpNode "ml-close-status" "Check ML close status" 700 300 '=https://ml-sync.zeabur.app/report/ml-close/status?period={{$json.period}}' 90000
  $routeCode = @'
const s = $input.first().json;
if (s.ready_for_finance) {
  return [{ json: { mode: 'run', url: '__TARGET_URL__' } }];
}
return [{ json: { mode: 'blocked', url: `https://ml-sync.zeabur.app/report/ml-close/card?period=${encodeURIComponent(s.period)}&send=true` } }];
'@.Replace("__TARGET_URL__", $targetUrl)
  $route = CodeNode "route-by-status" "Route by ML status" 940 300 $routeCode
  $call = HttpNode "execute-route" "Execute gated route" 1180 300 '={{$json.url}}' 180000
  $nodes = @($sched, (CodeNode "build-period" "Build period" 460 300 $periodCode), $status, $route, $call)
  $connections = @{}
  $connections[$sched.name] = @{ main = @(@(@{ node = "Build period"; type = "main"; index = 0 })) }
  $connections["Build period"] = @{ main = @(@(@{ node = "Check ML close status"; type = "main"; index = 0 })) }
  $connections["Check ML close status"] = @{ main = @(@(@{ node = "Route by ML status"; type = "main"; index = 0 })) }
  $connections["Route by ML status"] = @{ main = @(@(@{ node = "Execute gated route"; type = "main"; index = 0 })) }
  Update-Workflow $wf $nodes $connections
}

Update-GatedWorkflow "OzSSlkVa2b2y2aNS" "https://finance-report-audit.zeabur.app/aggregate"
Update-GatedWorkflow "aEzy1jZzG8lIEnss" "https://finance-report-audit.zeabur.app/report-monthly"
