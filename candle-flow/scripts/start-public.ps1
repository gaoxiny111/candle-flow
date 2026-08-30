# Start local API + Cloudflare tunnel for https://candle-flow.online
# Keep this window open (or run at login via Task Scheduler).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$cf = Join-Path $env:USERPROFILE "bin\cloudflared.exe"
$config = Join-Path $env:USERPROFILE ".cloudflared\config.yml"

$listening = Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Start-Process -FilePath (Join-Path $backend "venv\Scripts\uvicorn.exe") `
        -ArgumentList "app.main:app --host 127.0.0.1 --port 8002" `
        -WorkingDirectory $backend `
        -WindowStyle Minimized
    Start-Sleep -Seconds 3
}

& $cf tunnel --config $config run candle-flow
