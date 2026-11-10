# Installs the dashboard agent on Windows, with no console window ever.
#
#   irm https://pve.tail73491d.ts.net:8448/agents/install-windows-agent.ps1 -OutFile $env:TEMP\dash-install.ps1
#   powershell -ExecutionPolicy Bypass -File $env:TEMP\dash-install.ps1
#
# What changed from v1: the task used to launch powershell.exe directly, which
# flashed a console every minute. It now launches wscript.exe running a tiny
# .vbs with window style 0, so nothing appears at all.

$ErrorActionPreference = "Stop"
$base = "https://pve.tail73491d.ts.net:8448"
$dir  = Join-Path $env:USERPROFILE "dash"

Write-Host "== 1. agent =="
New-Item -ItemType Directory -Force $dir | Out-Null
Invoke-WebRequest "$base/agents/dash-agent.ps1" -OutFile (Join-Path $dir "dash-agent.ps1")
Write-Host "   $dir\dash-agent.ps1"

Write-Host ""
Write-Host "== 2. token =="
$token = $env:DASH_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    $existing = Join-Path $dir ".token"
    if (Test-Path $existing) {
        $token = (Get-Content $existing -Raw).Trim()
        Write-Host "   reusing the existing token file ($($token.Length) chars)"
    }
}
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "   On pve:  pct exec 200 -- sed -n 's/^DASH_TOKEN=//p' /opt/stack/dash/.env"
    $token = Read-Host -Prompt "   paste the token"
}
if ([string]::IsNullOrWhiteSpace($token)) { Write-Error "no token given"; exit 1 }

# Token file, readable by this account only.
$tokenPath = Join-Path $dir ".token"
[IO.File]::WriteAllText($tokenPath, $token.Trim())
try {
    icacls $tokenPath /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
} catch {
    Write-Host "   note: could not tighten ACLs on the token file"
}
Write-Host "   wrote $tokenPath ($($token.Trim().Length) chars)"

Write-Host ""
Write-Host "== 3. hidden launcher =="
# Window style 0 = hidden. wscript has no console of its own, so nothing flashes.
$vbsPath = Join-Path $dir "dash-agent.vbs"
$vbs = @'
Set sh = CreateObject("WScript.Shell")
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\dash\dash-agent.ps1"""
sh.Run cmd, 0, False
'@
[IO.File]::WriteAllText($vbsPath, $vbs)
Write-Host "   $vbsPath"

Write-Host ""
Write-Host "== 4. test push (visible this once) =="
$env:DASH_TOKEN = $token.Trim()
& (Join-Path $dir "dash-agent.ps1")

Write-Host ""
Write-Host "== 5. replace the task =="
schtasks /delete /tn "DashAgent" /f 2>$null | Out-Null
$tr = 'wscript.exe "' + $vbsPath + '"'
schtasks /create /tn "DashAgent" /sc minute /mo 1 /f /tr $tr | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "schtasks failed"; exit 1 }

schtasks /query /tn "DashAgent" /fo LIST 2>$null | Select-String "TaskName|Status|Next Run"

Write-Host ""
Write-Host "Done. Watch the clock for two minutes: no window should appear."
Write-Host "Remove later:  schtasks /delete /tn DashAgent /f"
