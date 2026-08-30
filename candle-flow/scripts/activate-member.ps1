# Activate membership for a registered phone (manual billing).
# Usage:
#   .\scripts\activate-member.ps1 -Username 13800138000 -Plan month
#   .\scripts\activate-member.ps1 -Username 13800138000 -Plan year
#   .\scripts\activate-member.ps1 -Username 13800138000 -Plan lifetime
#   .\scripts\activate-member.ps1 -Username 13800138000 -Plan free
# Optional: -BaseUrl http://127.0.0.1:8002 -Days 45

param(
    [Parameter(Mandatory = $true)][string]$Username,
    [ValidateSet("free", "month", "year", "lifetime")][string]$Plan = "month",
    [int]$Days = 0,
    [string]$BaseUrl = "http://127.0.0.1:8002"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root "backend\.env"
$key = ""
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*MEMBERSHIP_ADMIN_KEY\s*=\s*(.+)\s*$') {
        $key = $Matches[1].Trim().Trim('"').Trim("'")
    }
}
if (-not $key) {
    throw "MEMBERSHIP_ADMIN_KEY not set in backend/.env"
}

$body = @{
    admin_key = $key
    username  = $Username
    plan      = $Plan
}
if ($Days -gt 0) {
    $body.days = $Days
}

$json = $body | ConvertTo-Json
$res = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/admin/membership" -ContentType "application/json" -Body $json
$res | ConvertTo-Json -Depth 6
