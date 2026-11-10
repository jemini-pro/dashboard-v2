<#
.SYNOPSIS
    Dashboard agent for Windows. Posts CPU, memory, disk and uptime to the
    homelab dashboard.

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

$disk = Get-PSDrive -Name C
$diskUsed = $disk.Used
$diskTotal = $disk.Used + $disk.Free

$uptime = ((Get-Date) - $os.LastBootUpTime).TotalSeconds

$body = @{
    name      = $Name
    kind      = "pc"
    cpu_pct   = [math]::Round([double]$cpu2, 1)
    mem_pct   = [math]::Round(100.0 * $memUsed / $memTotal, 1)
    mem_used  = $memUsed
    mem_total = $memTotal
    disk_pct  = [math]::Round(100.0 * $diskUsed / $diskTotal, 1)
    uptime_s  = [int]$uptime
} | ConvertTo-Json

try {
    $res = Invoke-RestMethod -Uri "$DashUrl/api/push/$Name" -Method Post -Body $body `
        -ContentType "application/json" -Headers @{ "X-Dash-Token" = $DashToken } -TimeoutSec 15
    Write-Output "pushed: $($res.stored)"
}
catch {
    Write-Error "push failed: $($_.Exception.Message)"
    exit 1
}
