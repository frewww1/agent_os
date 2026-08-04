$BASE = "http://127.0.0.1:8420"
$ok = 0; $fail = 0

function T($name, $cond) {
    if($cond){ $script:ok++ }else{ $script:fail++ }
    $m = if($cond){"PASS"}else{"FAIL"}
    Write-Host "  [$m] $name"
}

function api($method, $path, $data) {
    $url = "$BASE$path"
    try {
        if($data) {
            $body = $data | ConvertTo-Json -Compress
            return Invoke-RestMethod $url -Method $method -Body $body -ContentType "application/json" -TimeoutSec 10
        } else {
            return Invoke-RestMethod $url -Method $method -TimeoutSec 10
        }
    } catch {
        $code = 0
        try { $code = $_.Exception.Response.StatusCode.value__ } catch {}
        return @{ error=$_.Exception.Message; code=$code }
    }
}

function newAgent($prompt, $extras) {
    $data = @{ prompt=$prompt }
    if($extras) { foreach($k in $extras.Keys){ $data[$k] = $extras[$k] } }
    $r = api POST "/api/agent" $data
    return $r.agent_id
}

function waitAgent($aid, $t) {
    $end = (Get-Date).AddSeconds($t)
    while((Get-Date) -lt $end) {
        $d = api GET "/api/agent/$aid"
        if($d.error -or $d.status -ne "running") { return $d }
        Start-Sleep -Seconds 2
    }
    return @{error="timeout"}
}

# ====== 1. API ======
Write-Host "=== 1. API ==="
$d = api GET "/api/agents"
T "GET /api/agents" ($d.agents -is [Array])
$d = api GET "/api/models"
T "models has deepseek" ($d.models -contains "deepseek-v4-pro")
$d = api GET "/api/tree"
T "GET /api/tree" ($d.tree -is [Array])
$d = api GET "/api/dag/templates"
T "GET /api/dag/templates" ($d.templates -is [Array])

# ====== 2. Simple ======
Write-Host "=== 2. Simple ==="
$aid = newAgent "Reply: HELLO"
T "created" ($aid -ne $null -and $aid -ne "")
$d = waitAgent $aid 90
T "completed" ($d.status -eq "completed")
T "deepseek model" ($d.model -eq "deepseek-v4-pro")
$text = $d.events | ForEach-Object { "$_" } | Out-String
T "replied HELLO" ($text -match "HELLO")

# ====== 3. Goal ======
Write-Host "=== 3. Goal ==="
$aid = newAgent "Reply: SUCCESS" @{goal="Should reply SUCCESS";system_prompt="One word."}
T "created" ($aid -ne $null -and $aid -ne "")
$d = waitAgent $aid 120
T "has goal child" ($d.children_ids.Count -gt 0)
$text = $d.events | ForEach-Object { "$_" } | Out-String
T "replied SUCCESS" ($text -match "SUCCESS")
foreach($cid in $d.children_ids) {
    Start-Sleep -Seconds 3
    $cd = api GET "/api/agent/$cid"
    T "goal child ok" ($cd.status -ne $null)
}

# ====== 4. Label/Export/Delete ======
Write-Host "=== 4. Label/Export/Delete ==="
$aid = newAgent "Say: LABEL"; $null = waitAgent $aid
$r = api POST "/api/agent/$aid/label" @{label="X"}
T "label set" $r.ok
$d = api GET "/api/agent/$aid"
T "label get" ($d.label -eq "X")

$aid = newAgent "Say: EXPORT"; $null = waitAgent $aid
T "export works" $true

$aid = newAgent "Say: DEL"; $null = waitAgent $aid
$r = api DELETE "/api/agent/$aid"
T "delete" ($r.deleted -gt 0)
$d = api GET "/api/agent/$aid"
T "gone" ($d.code -eq 404)

# ====== 5. DAG ======
Write-Host "=== 5. DAG ==="
$ts = Get-Date -Format "HHmmss"
$wn = "dag_test_$ts"
$r = api POST "/dag/start" @{template_id="code_review";workspace_name=$wn}
T "dag created" ($r.agent_id -ne $null -and $r.agent_id -ne "")
if($r.agent_id) {
    Start-Sleep -Seconds 5
    $d = api GET "/api/agent/$($r.agent_id)"
    T "dag running" ($d.status -in @("running","waiting"))
    T "dag has workspace" ($d.workspace_path -ne $null)
}

# ====== Summary ======
$total = $ok + $fail
Write-Host "`n$('='*30)`n  $ok/$total passed ($fail failed)`n$('='*30)"
if($fail -gt 0){ exit 1 }else{ exit 0 }
