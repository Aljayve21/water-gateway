<#
    office_probe.ps1  -  no-code diagnostic for the Innotech / NeoView BACnet network
    ---------------------------------------------------------------------------
    Requires NOTHING: no Python, no pip, no modules, no admin.
    Works on Windows PowerShell 5.1 (Windows 7/10/2016+) and PowerShell 7.

    HOW TO RUN ON THE OFFICE SERVER
      EASIEST: copy RUN_PROBE.bat and this file into the same folder on
      the office server, then DOUBLE-CLICK RUN_PROBE.bat. Done.

      MANUAL: open PowerShell (Start -> type "powershell" -> Enter) and
      paste one line:

         powershell -ExecutionPolicy Bypass -File .\office_probe.ps1

      or, if the file is on the Desktop:

         powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\office_probe.ps1"

    The screen output is the report. A copy is also written next to the
    script as office_probe_<timestamp>.txt - send that file back to me.

    USEFUL FLAGS
      -UnicastDevices 192.168.0.40,192.168.0.132
          Skip broadcast and ask those specific controllers directly. This
          is the decisive test, because broadcast does not cross a router.
      -ListenSeconds 8
          Give slow controllers more time to answer.
      -ModbusUnit 2 / -ModbusFunction 4
          Read input registers instead of holding registers.

    Notes
      * 100% read-only. Sends only Who-Is (a discovery broadcast) and
        Modbus READ HOLDING REGISTERS (function 04). It never writes.
      * UDP is used, so no firewall prompt blocks it (unlike TCP).
      * If PowerShell says "running scripts is disabled", always use the
        -ExecutionPolicy Bypass flag shown above. Do not change the
        machine-wide execution policy.
#>

[CmdletBinding()]
param(
    # Seconds to listen for I-Am replies after each transmission.
    [int] $ListenSeconds = 3,
    # Extra device instances to unicast-Who-Is to (comma separated) - usually empty.
    [string] $UnicastDevices = '',
    [string] $ModbusHost = '192.168.0.120',
    [int]    $ModbusPort = 502,
    # Modbus unit id to probe. 2 is the unit that answered previously.
    [int]    $ModbusUnit = 2,
    [int]    $ModbusAddress = 0,
    [int]    $ModbusCount = 20,
    # 3 = read HOLDING registers (this is where the Innotech data lives)
    # 4 = read INPUT registers (verified empty on this server)
    [int]    $ModbusFunction = 3
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$BacnetPort = 47808
$timestamp = (Get-Date).ToString('yyyyMMdd_HHmmss')
$report = New-Object System.Collections.Generic.List[string]

function Say([string]$line = '') {
    Write-Host $line
    $report.Add($line) | Out-Null
}
function Rule([string]$title) {
    Say ''
    Say ('=' * 74)
    Say $title
    Say ('=' * 74)
}
function Test-Administrator {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

# ---------------------------------------------------------------------------
Say ('OFFICE NETWORK PROBE   ' + (Get-Date))
Say ('Computer: ' + $env:COMPUTERNAME + '   User: ' + $env:USERNAME)
Say ('Administrator rights: ' + (Test-Administrator))

# ---------------------------------------------------------------------------
Rule '1. NETWORK INTERFACES (which subnets is this machine on?)'
$ipInfo = @()
try {
    $ipInfo = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -ne '127.0.0.1' })
} catch {
    $ipInfo = @(Get-WmiObject Win32_NetworkAdapterConfiguration -ErrorAction SilentlyContinue |
        Where-Object { $_.IPEnabled } |
        ForEach-Object { $ip = $_.IPAddress | Where-Object { $_ -notlike '127.*' } }
        ForEach-Object { [pscustomobject]@{ IPAddress = $_; InterfaceAlias = $_.Description } })
}
if ($ipInfo.Count -eq 0) {
    Say '!! No IPv4 interface reported.'
}
foreach ($i in $ipInfo) {
    Say ('  {0,-16} {1,-32} prefix={2}' -f $i.IPAddress, $i.InterfaceAlias, $i.PrefixLength)
}
$myIps = @($ipInfo | ForEach-Object { $_.IPAddress })
# Subnet-directed broadcast address for each /24, e.g. 192.168.0.122 -> 192.168.0.255
$subnetBroadcasts = @()
foreach ($ip in $myIps) {
    $parts = $ip -split '\.'
    if ($parts.Count -eq 4 -and $parts[0] -ne '169') {
        $b = $parts[0] + '.' + $parts[1] + '.' + $parts[2] + '.255'
        if ($subnetBroadcasts -notcontains $b) { $subnetBroadcasts += $b }
    }
}
Say ('Detected subnet broadcasts: ' + ($subnetBroadcasts -join ', '))
Say 'NOTE: If this machine is NOT on 192.168.0.x it cannot see the Innotech'
Say '      controllers by broadcast. That alone explains a silent scan.'

# ---------------------------------------------------------------------------
Rule '2. IS ANY BACNET / MODBUS / WEB SERVICE LISTENING? (netstat)'
Say 'TCP servers appear as LISTENING. UDP never does, so UDP rows are'
Say 'printed from the raw table instead. Only local listeners are shown.'
$ns = netstat -ano
foreach ($p in @($BacnetPort, 47809, $ModbusPort, 8080, 80, 443)) {
    $listen = @($ns | Select-String -Pattern ('LISTENING\s+\d+\s*$') |
        Where-Object { $_.ToString() -match (':' + $p + '\s') })
    $udp = @($ns | Select-String -Pattern ('^UDP\s' + '.*:' + $p + '\s') |
        Where-Object { $_.ToString() -notmatch '127\.0\.0\.1:' })
    if ($listen.Count -eq 0 -and $udp.Count -eq 0) {
        Say ('  port ' + $p.ToString().PadLeft(5) + ' : nothing local')
        continue
    }
    Say ('  port ' + $p.ToString().PadLeft(5) + ' :')
    foreach ($l in ($listen | Select-Object -First 4)) {
        $f = ($l.ToString().Trim() -split '\s+')
        Say ('      TCP LISTENING  ' + $f[1] + '  PID ' + $f[4])
    }
    foreach ($l in ($udp | Select-Object -First 4)) {
        Say ('      UDP (no LISTENING state on UDP)  ' + $l.ToString().Trim())
    }
}

# ---------------------------------------------------------------------------
Rule '3. WHICH PROCESS OWNS THE INNOTECH PORTS?'
# Turns PIDs from step 2 into process names, so we can confirm the platform.
# Only LISTENING sockets tell us who HOSTS a service. Established
# connections are just this machine talking to something else, so they are
# excluded to keep the output readable.
$pids = @()
foreach ($p in @(502, 1911, 4911, $BacnetPort, 8080, 80, 443)) {
    foreach ($l in (netstat -ano | Select-String -Pattern ('LISTENING\s+\d+\s*$'))) {
        $f = ($l.ToString().Trim() -split '\s+')
        # f[0]=Proto f[1]=Local f[2]=Foreign f[3]=State f[4]=PID
        if ($f.Count -ge 5 -and $f[1] -match (':' + $p + '$')) { $pids += $f[4] }
    }
}
$pids = $pids | Sort-Object -Unique
if ($pids.Count -eq 0) {
    Say '  (no local listeners on those ports - this server is a CLIENT of the'
    Say '   controllers, it does not host them. That is normal and expected.)'
} else {
    Say ('  Distinct PIDs found: ' + ($pids -join ', '))
    foreach ($procId in $pids) {
        $procName = '?'
        try { $procName = (Get-Process -Id ([int]$procId) -ErrorAction Stop).ProcessName } catch { }
        Say ('      PID ' + $procId.PadLeft(6) + '  ->  ' + $procName)
    }
    Say ''
    Say '  If a process above is java.exe / Niagara*, that IS the NeoView'
    Say '  platform. Note it down - it confirms what port 502 belongs to.'
}

# ---------------------------------------------------------------------------
Rule '4. ARP TABLE (who else is on this LAN?)'
arp -a

# ---------------------------------------------------------------------------
Rule '5. BACnet Who-Is (read-only discovery broadcast, UDP)'
Say 'Sending an unconfirmed Who-Is. Devices answer with I-Am. Purely read-only.'
Say ''

function New-WhoIsBytes {
    # Unconfirmed-Request (0x10), service Who-Is (0x08), with low/high
    # device-instance limits 0..4194302 so every device is included.
    return [byte[]]@(0x10, 0x08, 0x00, 0x00, 0xFF, 0xFF)
}

function Read-IAm {
    param($Bytes, [string]$FromIp)
    $text = ($Bytes | ForEach-Object { $_.ToString('X2') }) -join ' '
    # I-Am starts with 0x10 0x00, then the device object identifier
    # (tag 0xC4, type 0x0008) then the 16-bit device instance.
    $device = 0; $maxApdu = 0; $found = $false
    for ($i = 0; $i -lt $Bytes.Length - 5; $i++) {
        if ($Bytes[$i] -eq 0xC4 -and $Bytes[$i + 1] -eq 0x00 -and $Bytes[$i + 2] -eq 0x08) {
            $device = [int]$Bytes[$i + 3] * 256 + [int]$Bytes[$i + 4]
            if ($i + 8 -lt $Bytes.Length) { $maxApdu = [int]$Bytes[$i + 6] * 256 + [int]$Bytes[$i + 7] }
            $found = $true
            break
        }
    }
    if ($found) {
        Say ('  I-Am  from {0,-16} device-instance {1,-8} max-APDU {2}' -f $FromIp, $device, $maxApdu)
    } else {
        Say ('  reply from {0} (not an I-Am): {1}' -f $FromIp, $text)
    }
    [pscustomobject]@{ Found = $found; Device = $device }
}

function Send-WhoIs {
    param([string]$Target, [int]$ListenSec)
    $udp = New-Object System.Net.Sockets.UdpClient
    $udp.Client.SetSocketOption([System.Net.Sockets.SocketOptionLevel]::Socket,
                                [System.Net.Sockets.SocketOptionName]::ReuseAddress, $true)
    $udp.Client.ReceiveTimeout = 1500
    $udp.EnableBroadcast = $true
    try { $udp.Client.Bind((New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0))) }
    catch { Say ('  cannot bind a UDP socket: ' + $_.Exception.Message); $udp.Close(); return }
    $seen = @{}
    try {
        $data = New-WhoIsBytes
        $bytesSent = $udp.Send($data, $data.Length, $Target, $BacnetPort)
        Say ('  -> Who-Is sent to ' + $Target + ':' + $BacnetPort + ' (' + $bytesSent + ' bytes), listening ' + $ListenSec + 's')
        $deadline = (Get-Date).AddSeconds($ListenSec)
        while ((Get-Date) -lt $deadline) {
            $remaining = [int]([Math]::Max(200, ($deadline - (Get-Date)).TotalMilliseconds))
            $udp.Client.ReceiveTimeout = $remaining
            try {
                $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
                $resp = $udp.Receive([ref]$ep)
                if ($ep.Address.ToString() -notlike '127.*') {
                    $r = Read-IAm -Bytes $resp -FromIp $ep.Address.ToString()
                    if ($r.Found) { $seen[$r.Device] = $ep.Address.ToString() }
                }
            } catch [System.Net.Sockets.SocketException] { break }
        }
    } finally { $udp.Close() }
    return $seen
}

$total = 0
$targets = @('255.255.255.255') + $subnetBroadcasts
foreach ($t in $targets) {
    Say ''
    Say ('  -- broadcast target ' + $t + ' --')
    $r = Send-WhoIs -Target $t -ListenSec $ListenSeconds
    if ($null -ne $r) { $total += @($r.Keys).Count }
}

if ($UnicastDevices -ne '') {
    foreach ($d in ($UnicastDevices -split ',')) {
        $d = $d.Trim()
        if ($d -eq '') { continue }
        Say ''
        Say ('  -- unicast Who-Is to ' + $d + ' --')
        $r = Send-WhoIs -Target $d -ListenSec $ListenSeconds
        if ($null -ne $r) { $total += @($r.Keys).Count }
    }
}

Say ''
if ($total -eq 0) {
    Say '  RESULT: no BACnet device answered.'
    Say '  Most likely cause: the Innotech controllers are on a DIFFERENT subnet'
    Say '  and this machine is not on 192.168.0.x, so the broadcast never reaches'
    Say '  them. Unicast-Who-Is to a known controller IP is the real test -'
    Say '  re-run with:  .\office_probe.ps1 -UnicastDevices 192.168.0.40,192.168.0.132'
} else {
    Say ('  RESULT: ' + $total + ' BACnet device(s) answered. NeoView/Innotech IS reachable.')
}

# ---------------------------------------------------------------------------
Rule ('6. MODBUS READ-ONLY PROBE  ' + $ModbusHost + ':' + $ModbusPort + ' unit ' + $ModbusUnit)
Say ('Function ' + $ModbusFunction + ' (read) only - this probe never writes.')
Say ('Reading ' + $ModbusCount + ' registers from address ' + $ModbusAddress + '.')
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $iar = $tcp.BeginConnect($ModbusHost, $ModbusPort, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne(3000)) { throw 'TCP connect timed out after 3s' }
    $tcp.EndConnect($iar)
    Say ('  TCP connect OK to ' + $ModbusHost + ':' + $ModbusPort)
    $stream = $tcp.GetStream()
    $stream.ReadTimeout = 4000

    $req = New-Object 'System.Collections.Generic.List[byte]'
    $req.AddRange([byte[]]@(0x00, 0x01, 0x00, 0x00, 0x00, 0x06,
                               [byte]$ModbusUnit, [byte]$ModbusFunction,
                               [byte](($ModbusAddress -shr 8) -band 0xFF), [byte]($ModbusAddress -band 0xFF),
                               [byte](($ModbusCount -shr 8) -band 0xFF), [byte]($ModbusCount -band 0xFF)))
    $stream.Write($req.ToArray(), 0, $req.Count)
    $buf = New-Object byte[] 512
    $n = $stream.Read($buf, 0, $buf.Length)
    $tcp.Close()
    if ($n -lt 9) {
        Say ('  short reply (' + $n + ' bytes): ' + (($buf[0..($n - 1)] | ForEach-Object { $_.ToString('X2') }) -join ' '))
    } else {
        $func = $buf[7]
        if ($func -eq ($ModbusFunction + 0x80)) {
            $ex = $buf[8]
            $why = switch ($ex) {
                0x01 { 'Illegal Function' } 0x02 { 'Illegal Data Address' }
                0x03 { 'Illegal Data Value' } 0x04 { 'Server Device Failure' }
                0x06 { 'Server Device Busy' }   default { ('Exception 0x{0:X2}' -f $ex) }
            }
            Say ('  Modbus EXCEPTION 0x{0:X2} - {1}' -f $ex, $why)
            if ($ex -eq 0x02) { Say '  -> That is the known map limit: registers end near address 54.' }
        } else {
            $bc = $buf[8]
            Say ('  Function ' + $func + ', ' + $bc + ' data bytes returned. Registers read:')
            for ($r = 0; $r -lt [int]($bc / 2); $r++) {
                $reg = $ModbusAddress + $r
                $val = [int]$buf[9 + $r * 2] * 256 + [int]$buf[10 + $r * 2]
                $asInt = if ($val -gt 32767) { $val - 65536 } else { $val }
                $tag = if ($val -eq 0) { '' }
                       elseif ($val -eq 65535) { '   <- sentinel / not implemented' }
                       else { '   <- non-zero' }
                Say ('    reg {0,-5} = {1,-6} (signed {2,-6}){3}' -f $reg, $val, $asInt, $tag)
            }
            $nonZero = 0
            for ($r = 0; $r -lt [int]($bc / 2); $r++) {
                if (([int]$buf[9 + $r * 2] * 256 + [int]$buf[10 + $r * 2]) -ne 0) { $nonZero++ }
            }
            Say ('  -> {0} of {1} registers are non-zero. Expect only a handful on this' -f $nonZero, [int]($bc / 2))
            Say '     server; the Innotech map holds NO meter totalizers or readings.'
        }
    }
} catch {
    Say ('  MODBUS PROBE FAILED: ' + $_.Exception.Message)
    Say '  A refused/timeout result means the Modbus server is not answering from here.'
}

# ---------------------------------------------------------------------------
Rule '7. SUMMARY'
Say ('  This machine subnets      : ' + ($myIps -join ', '))
Say ('  Broadcast targets tried   : ' + ($targets -join ', '))
Say ('  Local Innotech listeners  : ' + $(if ($pids.Count) { $pids -join ', ' } else { 'none (client only)' }))
Say ('  BACnet devices answered   : ' + $total)
Say ''
Say '  Send this whole report back to get the next step.'

# ---------------------------------------------------------------------------
$outFile = Join-Path $PSScriptRoot ('office_probe_' + $timestamp + '.txt')
try {
    $report | Out-File -FilePath $outFile -Encoding UTF8
    Say ''
    Say ('Report saved: ' + $outFile)
} catch {
    Say ''
    Say 'Could not save the report file - please copy the text above manually.'
}
Say ''
Say 'Done.'
