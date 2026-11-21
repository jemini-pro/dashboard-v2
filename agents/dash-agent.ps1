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
#
# DriveType alone is not enough. Google Drive for desktop mounts each account
# as a virtual fixed drive whose size and free space are the cloud account's
# quota, so it must not count as a disk attached to this machine. A real volume
# has a partition on a disk behind it; a virtual one has no
# Win32_LogicalDiskToPartition entry. If that query fails entirely, fall back
# to DriveType alone rather than dropping every drive.
$backed = @{}
foreach ($link in @(Get-CimInstance Win32_LogicalDiskToPartition -ErrorAction SilentlyContinue)) {
    if ($link.Dependent -match 'Win32_LogicalDisk\.DeviceID="([A-Za-z]:)"') {
        $backed[$matches[1].ToUpperInvariant()] = $true
    }
}
$hasDiskBehind = { param($id) $backed.Count -eq 0 -or $backed.ContainsKey(([string]$id).ToUpperInvariant()) }

$disks = @(Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.DriveType -eq 2 -or $_.DriveType -eq 3) -and
        $_.Size -gt 0 -and $null -ne $_.FreeSpace -and
        (& $hasDiskBehind $_.DeviceID)
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

# The summary gauge is the whole machine: used over total across every volume.
# Per-volume rows above still show which drive is the full one, and the server
# names it in the disk warning.
$diskTotalBytes = ($disks | Measure-Object -Property total -Sum).Sum
$diskUsedBytes  = ($disks | Measure-Object -Property used -Sum).Sum
$diskPct = if ($disks.Count -gt 0 -and $diskTotalBytes -gt 0) {
    [math]::Round(100.0 * $diskUsedBytes / $diskTotalBytes, 1)
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
# Field names mirror dash_common.py (CORE_METRICS + EXTRA_METRICS), the Python
# agents' one payload vocabulary. PowerShell cannot import a Python module, so
# server/test_agents.py cross-checks the keys below against that file.
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

# DASH_HEALTH=0 opts a machine out of all probes, matching the Python agents.
if ($env:DASH_HEALTH -ne "0") {
    # SMART through the storage cmdlets. HealthStatus is mapped to the wire's
    # PASSED/FAILED vocabulary; reallocated/pending counters are ATA concepts
    # and are omitted on Windows rather than fabricated. Every probe is best
    # effort: a missing cmdlet, permission error or odd disk omits the block.
    try {
        $smart = @(Get-PhysicalDisk -ErrorAction Stop | Select-Object -First 16 | ForEach-Object {
            $device = if ($_.FriendlyName) { [string]$_.FriendlyName } else { [string]$_.DeviceId }
            $health = if ($_.HealthStatus -eq "Healthy") { "PASSED" } else { [string]$_.HealthStatus }
            $entry = @{ device = $device; health = $health }
            try {
                $counter = Get-StorageReliabilityCounter -DeviceNumber ([int]$_.DeviceId) -ErrorAction Stop
                if ($null -ne $counter.Temperature) { $entry["temperature_c"] = [int]$counter.Temperature }
                if ($null -ne $counter.PowerOnHours) { $entry["power_on_hours"] = [int64]$counter.PowerOnHours }
            } catch { }
            $entry
        } | Where-Object { $_.device })
        if ($smart.Count -gt 0) { $body["smart"] = $smart }
    } catch { }

    # Pending updates need the PSWindowsUpdate module. Without it the whole
    # block is omitted, as with the other optional signals. Reboot detection is
    # a cheap registry probe included when the module is present.
    if (Get-Command Get-WindowsUpdate -ErrorAction SilentlyContinue) {
        try {
            $wu = @(Get-WindowsUpdate -ErrorAction Stop)
            $updates = @{ pending = $wu.Count }
            $security = @($wu | Where-Object {
                $_.MsrcSeverity -eq "Critical" -or $_.SecurityBulletinIDs
            }).Count
            if ($security -gt 0) { $updates["security"] = $security }
            $reboot = $false
            foreach ($key in @(
                "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
                "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
                "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\PendingFileRenameOperations"
            )) {
                try { if (Test-Path $key) { $reboot = $true; break } } catch { }
            }
            $updates["reboot_required"] = $reboot
            $body["updates"] = $updates
        } catch { }
    }
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
