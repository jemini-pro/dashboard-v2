<#
.SYNOPSIS
    Dashboard agent for Windows. Posts CPU, memory, every fixed/removable
    drive and uptime to the homelab dashboard.

.EXAMPLE
    $env:DASH_TOKEN = "paste-token"
    .\dash-agent.ps1

.EXAMPLE
    # every 5 minutes, for the current user, at logon
    schtasks /create /tn "DashAgent" /tr "powershell -NoProfile -WindowStyle Hidden -File $PWD\dash-agent.ps1" /sc minute /mo 5 /f
#>

$ErrorActionPreference = "Stop"

$DashUrl   = if ($env:DASH_URL)   { $env:DASH_URL }   else { "https://pve.tail73491d.ts.net:8448" }
$DashToken = $env:DASH_TOKEN
$Name      = if ($env:DASH_NAME)  { $env:DASH_NAME }  else { $env:COMPUTERNAME }

# Nobody revisits an installed agent: the scheduled task keeps running whatever
# the installer downloaded, so a fix on the server would otherwise never reach
# this machine. Pull the served copy each run and swap it in when it differs,
# then keep going with the code already loaded; the next run uses the new file.
# Best effort: an unreachable server must never stop the heartbeat.
# Set DASH_AGENT_UPDATE=0 to pin a development copy.
if ($PSCommandPath -and $env:DASH_AGENT_UPDATE -ne "0") {
    try {
        # identity keeps Caddy from gzipping it: .Content must be text, not a
        # compressed byte array, for the comparison below to mean anything.
        $served = (Invoke-WebRequest -Uri "$DashUrl/agents/dash-agent.ps1" `
            -UseBasicParsing -TimeoutSec 15 -Headers @{ "Accept-Encoding" = "identity" }).Content
        $current = Get-Content -LiteralPath $PSCommandPath -Raw
        if ($served -and $served.Trim() -ne $current.Trim()) {
            $next = "$PSCommandPath.new"
            [IO.File]::WriteAllText($next, $served)
            Move-Item -LiteralPath $next -Destination $PSCommandPath -Force
        }
    } catch {
        # Offline or a transient error: report with the copy we already have.
    }
}

# Fall back to a token file, so a hidden task needs no environment variable.
if (-not $DashToken) {
    $tokenFile = Join-Path $env:USERPROFILE "dash\.token"
    if (Test-Path $tokenFile) { $DashToken = (Get-Content $tokenFile -Raw).Trim() }
}

if (-not $DashToken) {
    Write-Error "No token: set DASH_TOKEN or write it to %USERPROFILE%\dash\.token"
    exit 1
}

$os = Get-CimInstance Win32_OperatingSystem

# CPU: sample twice 1s apart and use the delta.
$cpu1 = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentProcessorTime
Start-Sleep -Seconds 1
$cpu2 = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentProcessorTime

$memTotal = [int64]$os.TotalVisibleMemorySize * 1KB
$memFree  = [int64]$os.FreePhysicalMemory * 1KB
$memUsed  = $memTotal - $memFree

# Every fixed or removable volume, not just C:. DriveType 3 is a local fixed
# disk and 2 is removable (USB enclosure); 4 is a mapped network drive, which
# is not storage on this machine and would report another box's free space.
$disks = @(Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.DriveType -eq 2 -or $_.DriveType -eq 3) -and
        $_.Size -gt 0 -and $null -ne $_.FreeSpace
    } |
    Sort-Object DeviceID |
    ForEach-Object {
        $size = [int64]$_.Size
        $diskUsed = $size - [int64]$_.FreeSpace
        $label = if ($_.VolumeName) { [string]$_.VolumeName } else { [string]$_.DeviceID }
        [pscustomobject]@{
            mount = [string]$_.DeviceID + "\"
            label = $label
            total = $size
            used  = $diskUsed
            pct   = [math]::Round(100.0 * $diskUsed / $size, 1)
        }
    })

# The summary gauge follows the fullest volume, so a nearly full data or USB
# drive is not hidden behind a roomy system drive.
$diskPct = if ($disks.Count -gt 0) {
    ($disks | Measure-Object -Property pct -Maximum).Maximum
} else {
    $null
}

$uptime = ((Get-Date) - $os.LastBootUpTime).TotalSeconds

# Page file is the closest Windows equivalent to swap. If the machine has no
# page file, the metric is omitted rather than reported as zero.
$pageFile = $null
try {
    $pf = @(Get-CimInstance Win32_PageFileUsage -ErrorAction Stop)
    if ($pf.Count -gt 0) {
        $pfUsed  = ($pf | Measure-Object -Property CurrentUsage -Sum).Sum
        $pfTotal = ($pf | Measure-Object -Property AllocatedBaseSize -Sum).Sum
        if ($pfTotal -gt 0) {
            $pageFile = @{
                used  = [int64]$pfUsed * 1MB
                total = [int64]$pfTotal * 1MB
            }
        }
    }
} catch { }

# Network throughput counter names are localized; this is best effort. A
# missing counter path leaves the keys out of the payload.
$net = $null
try {
    $rx = (Get-Counter '\Network Interface(*)\Bytes Received/sec' -ErrorAction Stop).CounterSamples |
        Where-Object { $_.InstanceName -notmatch 'loopback' } |
        Measure-Object -Property CookedValue -Sum
    $tx = (Get-Counter '\Network Interface(*)\Bytes Sent/sec' -ErrorAction Stop).CounterSamples |
        Where-Object { $_.InstanceName -notmatch 'loopback' } |
        Measure-Object -Property CookedValue -Sum
    if ($null -ne $rx.Sum -and $null -ne $tx.Sum) {
        $net = @{ rx = [int64]$rx.Sum; tx = [int64]$tx.Sum }
    }
} catch { }

# Windows has no load average or iowait/steal, so those keys are never sent.
$body = @{
    name      = $Name
    kind      = "pc"
    cpu_pct   = [math]::Round([double]$cpu2, 1)
    mem_pct   = [math]::Round(100.0 * $memUsed / $memTotal, 1)
    mem_used  = $memUsed
    mem_total = $memTotal
    disk_pct  = $diskPct
    disks     = $disks
    uptime_s  = [int]$uptime
}

if ($pageFile) {
    $body["swap_used"] = $pageFile.used
    $body["swap_total"] = $pageFile.total
}
if ($net) {
    $body["net_rx_bps"] = $net.rx
    $body["net_tx_bps"] = $net.tx
}

$body = $body | ConvertTo-Json -Depth 4

try {
    $res = Invoke-RestMethod -Uri "$DashUrl/api/push/$Name" -Method Post -Body $body `
        -ContentType "application/json" -Headers @{ "X-Dash-Token" = $DashToken } -TimeoutSec 15
    Write-Output "pushed: $($res.stored)"
}
catch {
    Write-Error "push failed: $($_.Exception.Message)"
    exit 1
}
