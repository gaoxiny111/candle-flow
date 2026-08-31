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

robocopy (Join-Path $root "backend") (Join-Path $stage "backend") /E /XD venv venv_broken* __pycache__ .pytest_cache data /XF *.pyc .env /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
robocopy (Join-Path $root "frontend\dist") (Join-Path $stage "frontend\dist") /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
robocopy (Join-Path $root "scripts\deploy") (Join-Path $stage "scripts\deploy") /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null

$credSrc = Join-Path $env:USERPROFILE ".cloudflared\f3eb03d8-7017-488e-9176-79377668ea3f.json"
$credDst = Join-Path $stage "scripts\deploy\tunnel-credentials.json"
if (Test-Path $credSrc) {
  Copy-Item $credSrc $credDst -Force
} else {
  Write-Host "WARN: local tunnel credentials missing; keep server copy"
}

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
$remotePack = "~/candle-flow-deploy.tgz"
& scp.exe @common -P $Port $pack "${target}:${remotePack}"
if ($LASTEXITCODE -ne 0) { throw "scp failed: $LASTEXITCODE" }

$remote = @"
set -e
sudo mkdir -p /opt/candle-flow
sudo tar -xzf `$HOME/candle-flow-deploy.tgz -C /opt/candle-flow
sudo sed -i 's/\r`$//' /opt/candle-flow/scripts/deploy/remote-setup.sh
sudo chmod +x /opt/candle-flow/scripts/deploy/remote-setup.sh
sudo bash /opt/candle-flow/scripts/deploy/remote-setup.sh
rm -f `$HOME/candle-flow-deploy.tgz
"@ -replace "`r`n", "`n"
& ssh.exe @common -p $Port $target $remote
if ($LASTEXITCODE -ne 0) { throw "remote setup failed: $LASTEXITCODE" }
