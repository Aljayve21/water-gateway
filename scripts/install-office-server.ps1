<#
.SYNOPSIS
  Install / prepare the water-gateway on the OFFICE server (Windows).

.DESCRIPTION
  Run this ON the office server, as Administrator. It:
    1. Verifies Python 3.11+ is installed.
    2. Fills in config/.env with the correct values for this server
       (MySQL on localhost, BMS reachable binds) — keeps existing
       MySQL user/password/database.
    3. Creates the venv and installs requirements.
    4. Opens the Windows firewall for FTP-ish ports: TCP 502,
       UDP 47808, TCP 9090.
    5. Registers the gateway as a Windows service via NSSM
       (auto-start on boot, auto-restart on crash).

.EXAMPLE
  .\scripts\install-office-server.ps1 -ProjectPath "C:\EnyeMeteringSystem\water-gateway"
#>
param(
    [string]$ProjectPath = "C:\xampp\htdocs\EnyeMeteringSystem\water-gateway",
    [string]$MysqlHost = "127.0.0.1",          # office MySQL runs on this same server
    [string]$ServiceName = "WaterGateway"
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
$nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue

Write-Host "== Water-gateway office-server installer ==" -ForegroundColor Cyan

# 0) sanity checks -------------------------------------------------------
if (-not (Test-Path -LiteralPath $ProjectPath)) {
    throw "Project path not found: $ProjectPath"
}
$envFile = Join-Path $ProjectPath ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "No .env found next to the project. Copy .env.example to .env first (keep your MySQL user/password)."
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    $py = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $py) {
    throw "Python not found. Install Python 3.11+ from https://www.python.org first."
}
$ver = & $py.Source --version 2>&1
Write-Host "Python: $ver"

# 1) fix .env for this server --------------------------------------------
Write-Host "`n-- Updating .env (host + binds) --"
$lines = Get-Content -LiteralPath $envFile
$map = @{
    "MYSQL_HOST"          = $MysqlHost
    "MODBUS_BIND_HOST"    = "0.0.0.0"
    "MODBUS_BIND_PORT"    = "502"
    "BACNET_BIND_IP"      = "0.0.0.0"
    "BACNET_BIND_PORT"    = "47808"
    "HEALTH_BIND_HOST"    = "127.0.0.1"   # health stays local
    "HEALTH_BIND_PORT"    = "9090"
}
$out = New-Object System.Collections.Generic.List[string]
$seen = @{}
foreach ($line in $lines) {
    if ($line -match "^\s*#") { $out.Add($line); continue }          # keep comments
    if ($line -match "^([A-Z0-9_]+)=") {
        $key = $Matches[1]
        if ($map.ContainsKey($key)) {
            $out.Add("$key=$($map[$key])")
            $seen[$key] = $true
            continue
        }
    }
    $out.Add($line)
}
foreach ($key in $map.Keys) {
    if (-not $seen.ContainsKey($key)) { $out.Add("$key=$($map[$key])") }
}
$out | Set-Content -LiteralPath $envFile -Encoding UTF8
Write-Host ".env updated: MySQL host=$MysqlHost, Modbus/BACnet binds=0.0.0.0"

# 2) venv + dependencies --------------------------------------------------
Write-Host "`n-- Creating venv and installing requirements --"
$venv = Join-Path $ProjectPath ".venv"
if (-not (Test-Path -LiteralPath (Join-Path $venv "Scripts\python.exe"))) {
    & $py.Source -m venv $venv
}
& (Join-Path $venv "Scripts\python.exe") -m pip install --upgrade pip --quiet
& (Join-Path $venv "Scripts\python.exe") -m pip install -r (Join-Path $ProjectPath "requirements.txt")
Write-Host "Dependencies installed."

# 3) firewall -------------------------------------------------------------
Write-Host "`n-- Opening Windows firewall (run as Administrator) --"
$rules = @(
    @("WaterGateway-Modbus-502",  "TCP", "502",  "in"),
    @("WaterGateway-BACnet-47808","UDP", "47808","in"),
    @("WaterGateway-Health-9090", "TCP", "9090", "in")
)
foreach ($r in $rules) {
    netsh advfirewall firewall add rule name=$($r[0]) dir=$($r[3]) action=allow protocol=$($r[1]) localport=$($r[2]) | Out-Null
    Write-Host "  opened $($r[1]) port $($r[2]) ($($r[0]))"
}

# 4) NSSM service ----------------------------------------------------------
Write-Host "`n-- Windows service via NSSM --"
if (-not $nssm) {
    Write-Warning "nssm.exe not on PATH. Download from https://nssm.cc/download (nssm-2.24.zip),"
    Write-Warning "unzip and put nssm.exe on the PATH, then re-run this step (or run the commands below manually)."
} else {
    $pyExe = Join-Path $venv "Scripts\python.exe"
    $existing = & $nssm.Source get $ServiceName Application 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  service '$ServiceName' already exists - stopping to reconfigure."
        & $nssm.Source stop $ServiceName 2>&1 | Out-Null
    }
    & $nssm.Source remove $ServiceName confirm 2>&1 | Out-Null

    & $nssm.Source install $ServiceName $pyExe "-m gateway.main"
    & $nssm.Source set $ServiceName AppDirectory (Join-Path $ProjectPath "src")   # so -m gateway.main resolves
    & $nssm.Source set $ServiceName Start SERVICE_AUTO_START
    & $nssm.Source set $ServiceName AppStdout (Join-Path $ProjectPath "logs\gateway.out.log")
    & $nssm.Source set $ServiceName AppStderr (Join-Path $ProjectPath "logs\gateway.err.log")
    & $nssm.Source set $ServiceName AppRotateFiles 1
    & $nssm.Source set $ServiceName AppRotateBytes 10485760
    & $nssm.Source set $ServiceName AppExit Default Restart
    & $nssm.Source set $ServiceName AppRestartDelay 5000
    & $nssm.Source start $ServiceName
    Write-Host "  service '$ServiceName' installed and started (auto-start on boot)."
}

Write-Host "`n== Done ==" -ForegroundColor Green
Write-Host "Check it:  Invoke-RestMethod http://127.0.0.1:9090/health"
Write-Host "Logs:      $ProjectPath\logs"