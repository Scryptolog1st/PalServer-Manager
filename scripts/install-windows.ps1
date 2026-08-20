#Requires -RunAsAdministrator
param([switch]$EnableOpenSSH)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Install = Join-Path $env:ProgramData "PalServerManager"
$Venv = Join-Path $Install "venv"
$Config = Join-Path $Install "config.json"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.11+ is required. Install Python from python.org or use the packaged Windows release."
}

New-Item -ItemType Directory -Force -Path $Install | Out-Null

if ($EnableOpenSSH) {
    $cap = Get-WindowsCapability -Online | Where-Object Name -Like 'OpenSSH.Server*' | Select-Object -First 1
    if ($cap -and $cap.State -ne 'Installed') {
        Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    }
    Set-Service -Name sshd -StartupType Automatic
    Start-Service sshd
    Write-Host "OpenSSH Server enabled for SSH-tunnel remote management."
}
python -m venv $Venv
& "$Venv\Scripts\python.exe" -m pip install --upgrade pip
& "$Venv\Scripts\pip.exe" install "${Root}[all]"

$env:PALSERVER_MANAGER_CONFIG = $Config
& "$Venv\Scripts\python.exe" -c "from palserver_manager.config import load_config,save_config; c=load_config(); save_config(c); print('Agent token:',c.agent.token)"

# Restrict the token-bearing agent config to SYSTEM, Administrators and the installer user.
& icacls.exe $Config /inheritance:r /grant:r "SYSTEM:(F)" "Administrators:(F)" "$($env:USERNAME):(F)" | Out-Null

$AgentCmd = Join-Path $Install "agent.cmd"
@"
@echo off
set PALSERVER_MANAGER_CONFIG=$Config
"$Venv\Scripts\palserver-agent.exe" --host 127.0.0.1 --port 8765
"@ | Set-Content -Encoding ASCII $AgentCmd

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$AgentCmd`""
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "PalServer Manager Agent" -Action $Action -Trigger $Trigger -Principal $Principal -Force | Out-Null
Start-ScheduledTask -TaskName "PalServer Manager Agent"

$GuiScript = Join-Path $Install "gui.ps1"
@"
`$env:PALSERVER_MANAGER_CONFIG = '$Config'
& '$Venv\Scripts\pythonw.exe' -m palserver_manager.gui
"@ | Set-Content -Encoding UTF8 $GuiScript

$CliCmd = Join-Path $Install "palserver-manager.cmd"
@"
@echo off
set PALSERVER_MANAGER_CONFIG=$Config
"$Venv\Scripts\palserver-manager.exe" %*
"@ | Set-Content -Encoding ASCII $CliCmd

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'PalServer Manager.lnk'))
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$GuiScript`""
$Shortcut.WorkingDirectory = $Install
$Shortcut.Save()

$MachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if (($MachinePath -split ';') -notcontains $Install) {
    [Environment]::SetEnvironmentVariable('Path', ($MachinePath.TrimEnd(';') + ';' + $Install), 'Machine')
}

Write-Host ""
Write-Host "PalServer Manager installed."
Write-Host "Windows GUI shortcut created on the desktop."
Write-Host "Agent is bound to 127.0.0.1:8765 and starts at boot."
Write-Host "Config: $Config"
Write-Host "Use SSH-tunnel mode for secure off-network administration."
if (-not $EnableOpenSSH) { Write-Host "Tip: rerun with -EnableOpenSSH if this Windows host should accept SSH tunnels." }
