param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string]$User = "root",
  [int]$Port = 22
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pack = Join-Path $env:TEMP "candle-flow-deploy.tgz"
$stage = Join-Path $env:TEMP "candle-flow-stage"
$key = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
$common = @("-i", $key, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

robocopy (Join-Path $root "backend") (Join-Path $stage "backend") /E /XD venv __pycache__ .pytest_cache /XF *.pyc /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
robocopy (Join-Path $root "frontend\dist") (Join-Path $stage "frontend\dist") /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
robocopy (Join-Path $root "scripts\deploy") (Join-Path $stage "scripts\deploy") /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null

$credSrc = Join-Path $env:USERPROFILE ".cloudflared\f3eb03d8-7017-488e-9176-79377668ea3f.json"
Copy-Item $credSrc (Join-Path $stage "scripts\deploy\tunnel-credentials.json") -Force

$setup = Join-Path $stage "scripts\deploy\remote-setup.sh"
$unix = [IO.File]::ReadAllText($setup) -replace "`r`n", "`n" -replace "`r", "`n"
[IO.File]::WriteAllText($setup, $unix)

if (Test-Path $pack) { Remove-Item -Force $pack }
Push-Location $stage
try {
  tar -czf $pack *
} finally {
  Pop-Location
}

$target = "${User}@${Server}"
& scp.exe @common -P $Port $pack "${target}:/tmp/candle-flow-deploy.tgz"
if ($LASTEXITCODE -ne 0) { throw "scp failed: $LASTEXITCODE" }

$remote = "mkdir -p /opt/candle-flow && tar -xzf /tmp/candle-flow-deploy.tgz -C /opt/candle-flow && sed -i 's/\r$//' /opt/candle-flow/scripts/deploy/remote-setup.sh && chmod +x /opt/candle-flow/scripts/deploy/remote-setup.sh && bash /opt/candle-flow/scripts/deploy/remote-setup.sh"
& ssh.exe @common -p $Port $target $remote
if ($LASTEXITCODE -ne 0) { throw "remote setup failed: $LASTEXITCODE" }
