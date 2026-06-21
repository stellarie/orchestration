# smoke.ps1 - start the orchestration server and verify it responds
# Usage: .\\.claude\skills\run-orchestration\smoke.ps1 [-Stop]
# Run from C:\Users\Stella\orchestration\

param([switch]$Stop)

$port    = 8765
$pidFile = Join-Path $PSScriptRoot "server.pid"

function Stop-Server {
    if (Test-Path $pidFile) {
        $id = Get-Content $pidFile
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        Write-Host "Server stopped (pid $id)."
    } else {
        Write-Host "No pid file found, nothing to stop."
    }
}

if ($Stop) { Stop-Server; exit 0 }

# Start server in background
$workDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$proc = Start-Process -FilePath "python" `
    -ArgumentList "-m", "uvicorn", "orchestration_server:app", "--host", "127.0.0.1", "--port", "$port" `
    -WorkingDirectory $workDir `
    -PassThru -WindowStyle Hidden
$proc.Id | Set-Content $pidFile

# Wait for ready (up to 10s)
$deadline = (Get-Date).AddSeconds(10)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -Method GET -ErrorAction Stop
        if ($r.status -eq "ok") { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}

if (-not $ready) { Write-Host "ERROR: server did not start in 10s"; exit 1 }

# Smoke checks
$health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -Method GET
Write-Host "Health: $($health.status)"

$body = @{ repo_path = "C:\nonexistent"; description = "smoke" } | ConvertTo-Json
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:$port/task/init" -Method POST -Body $body -ContentType "application/json" | Out-Null
    Write-Host "POST /task/init: unexpected 200"
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 400) { Write-Host "POST /task/init: 400 (path validation ok)" }
    else { Write-Host "POST /task/init: unexpected $code"; exit 1 }
}

Write-Host "Smoke passed. Server on port $port (pid $($proc.Id))."
Write-Host "Stop: .\.claude\skills\run-orchestration\smoke.ps1 -Stop"
