# After: cloudflared tunnel login (browser).
# Binds candle-flow.online to this PC via Cloudflare Tunnel.

$ErrorActionPreference = "Stop"
$cf = Join-Path $env:USERPROFILE "bin\cloudflared.exe"
$cfdir = Join-Path $env:USERPROFILE ".cloudflared"
$cert = Join-Path $cfdir "cert.pem"

if (-not (Test-Path $cf)) { throw "cloudflared.exe missing: $cf" }
if (-not (Test-Path $cert)) { throw "还没有登录 Cloudflare，请先完成浏览器里的授权。" }

New-Item -ItemType Directory -Force -Path $cfdir | Out-Null

$tunnels = & $cf tunnel list 2>&1 | Out-String
if ($tunnels -notmatch "candle-flow") {
    & $cf tunnel create candle-flow
}

$cred = Get-ChildItem $cfdir -Filter "*.json" |
    Where-Object { $_.Name -match "^[0-9a-f-]{36}\.json$" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $cred) { throw "找不到隧道凭证 json" }

$uuid = [System.IO.Path]::GetFileNameWithoutExtension($cred.Name)
$configPath = Join-Path $cfdir "config.yml"
@"
tunnel: $uuid
credentials-file: $($cred.FullName)

ingress:
  - hostname: candle-flow.online
    service: http://127.0.0.1:8002
  - hostname: www.candle-flow.online
    service: http://127.0.0.1:8002
  - service: http_status:404
"@ | Set-Content -Encoding utf8 $configPath

Write-Output "TUNNEL_UUID=$uuid"
Write-Output "CNAME_TARGET=$uuid.cfargotunnel.com"
Write-Output "CONFIG=$configPath"

try {
    & $cf tunnel route dns candle-flow candle-flow.online
    & $cf tunnel route dns candle-flow www.candle-flow.online
} catch {
    Write-Output "DNS_ROUTE_SKIPPED: 把域名加到 Cloudflare 后可再执行 route dns。阿里云可先加 CNAME -> $uuid.cfargotunnel.com"
}
