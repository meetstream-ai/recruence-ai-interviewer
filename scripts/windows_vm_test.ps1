<#
Recruence installer test, for a CLEAN Windows VM only (release validation B-8). Never run it on a machine with a
Recruence install you care about: it installs, upgrades and uninstalls Recruence for the current user.

  powershell -ExecutionPolicy Bypass -File windows_vm_test.ps1 -OldInstaller .\Recruence-Setup-0.12.0.exe -NewInstaller .\Recruence-Setup-0.13.2.exe

What it does, and checks:
  1. The VM is clean (no Recruence installed, no data folder).
  2. Silent install of the old version: files, shortcuts, the uninstall entry and its version.
  3. Launch: the backend answers on 127.0.0.1:8765 (time to ready). It then PAUSES for you to finish the first-run
     setup in the window, create an account, a role, and close the app.
  4. Closing leaves no Recruence or backend processes.
  5. Silent upgrade to the new version: the version changes, the data folder is kept (same database file).
  6. Launch again: the backend answers, setup is already complete. PAUSES for you to check you're still signed in
     and the role is there, then close the app.
  7. Silent uninstall: program files, shortcuts and the uninstall entry are gone, no processes are left, and the data
     folder is kept (the default answer to "also delete your data?").
Writes every result to windows_vm_test_result.txt next to the script.

  -Unattended  for a fresh CI runner (.github/workflows/b8-installer.yml): the two pauses become the same steps through
               the local API the setup wizard uses (a throwaway account, setup finished without an AI, a role), and the
               app is closed the way its window's close button does (a close message, never a forced kill). After the
               upgrade, the same sign-in session must still work and the role must still be there.
#>
param(
    [Parameter(Mandatory = $true)][string]$OldInstaller,
    [Parameter(Mandatory = $true)][string]$NewInstaller,
    [switch]$Unattended
)
$ErrorActionPreference = "Stop"
$log = Join-Path $PSScriptRoot "windows_vm_test_result.txt"
"Recruence installer test, $(Get-Date -Format s), $([Environment]::OSVersion.VersionString)" | Set-Content -Encoding utf8 $log
$failures = 0
function Result([string]$name, [bool]$ok, [string]$detail = "") {
    $line = "{0}  {1}  {2}" -f ($(if ($ok) { "PASS" } else { "FAIL" })), $name, $detail
    if (-not $ok) { $script:failures++ }
    Write-Host $line; Add-Content -Encoding utf8 $log $line
}
# The program folder comes from the uninstall entry: an upgrade keeps an older install's folder (installs from before
# the rename live in Programs\MIA Interviewer). Before any install, the usual one is assumed.
$installDir = Join-Path $env:LOCALAPPDATA "Programs\Recruence"
$exe = Join-Path $installDir "Recruence.exe"
function RefreshPaths {
    $e = UninstallEntry
    if ($e -and $e.DisplayIcon) {
        $script:exe = ($e.DisplayIcon -replace ',\d+$', '').Trim('"')
        $script:installDir = Split-Path $script:exe
    }
}
$dataDir = Join-Path $env:APPDATA "Recruence"
$desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "Recruence.lnk"
$startLink = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Recruence.lnk"
function UninstallEntry { Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "Recruence*" } | Select-Object -First 1 }
function Leftovers { @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -in @("Recruence", "mia-backend") }) }
function WaitBackend([int]$seconds = 120) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 3; if ($h.ok) { return [math]::Round($sw.Elapsed.TotalSeconds, 1) } } catch {}
        Start-Sleep -Milliseconds 500
    }
    return -1
}
function InstallSilently([string]$installer) {
    $p = Start-Process -FilePath $installer -ArgumentList "/S" -PassThru -Wait
    return $p.ExitCode
}
$api = "http://127.0.0.1:8765"
$session = $null
$roleTitle = "B-8 Installer Test Role"
function Json($method, $path, $body = $null) {
    $req = @{ Uri = "$api$path"; Method = $method; WebSession = $script:session; UseBasicParsing = $true; TimeoutSec = 30 }
    if ($null -ne $body) { $req.Body = ($body | ConvertTo-Json -Compress); $req.ContentType = "application/json" }
    return Invoke-WebRequest @req
}
function SetUpThroughTheApi {
    # What a person does in the first-run wizard, as tests/ui/test_dashboard_ui.py does it: run without an AI model, so
    # nothing external is called. The password is generated here and never written anywhere.
    $script:session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $pw = [Convert]::ToBase64String((1..18 | ForEach-Object { [byte](Get-Random -Maximum 256) }))
    Json PUT "/api/setup/state" @{ mode = "personal" } | Out-Null
    Json POST "/api/auth/register" @{ name = "B8 Tester"; email = "b8-tester@example.com"; password = $pw; workspace_name = "B-8" } | Out-Null
    Json PUT "/api/setup/state" @{ ai_offline = $true } | Out-Null
    Json POST "/api/setup/complete" | Out-Null
    Json POST "/api/roles" @{ role_title = $roleTitle; job_description = "Installer test." } | Out-Null
}
function RoleStillThere {
    try { $r = Json GET "/api/roles"; return ($r.StatusCode -eq 200) -and ($r.Content -like "*$roleTitle*") } catch { return $false }
}
Add-Type -Namespace B8 -Name Win -MemberDefinition '[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr h);'
function Windows { @(Get-Process -Name Recruence -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }) }
function Describe($procs) { ($procs | ForEach-Object { "$($_.ProcessName)#$($_.Id)" + $(if ($_.MainWindowHandle -ne 0) { " window '$($_.MainWindowTitle)' visible=$([B8.Win]::IsWindowVisible($_.MainWindowHandle))" } else { "" }) }) -join ", " }
function CloseApp {
    # A close message to the app's window (WM_CLOSE), as clicking its X does; the app then stops its backend. Only the
    # main Electron process has a window: its helpers (GPU, renderer) are also Recruence.exe and close with it. As a
    # person would, wait until the window is on screen first: it's created hidden and shown once its page is ready.
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 30 -and -not (Windows | Where-Object { [B8.Win]::IsWindowVisible($_.MainWindowHandle) })) { Start-Sleep -Milliseconds 500 }
    $windows = Windows
    $seen = Describe $windows
    foreach ($p in $windows) { [void]$p.CloseMainWindow() }
    Result "Closed through its window" ($windows.Count -gt 0) "$seen, after $([math]::Round($sw.Elapsed.TotalSeconds, 1)) s"
}
function WindowText([IntPtr]$handle) {
    # The text of a window (e.g. Electron's "Error" dialog for an uncaught error in the app's main process), read with
    # UI Automation: on a CI runner nobody can look at it.
    try {
        Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
        $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        return (($all | ForEach-Object { $_.Current.Name } | Where-Object { $_ } | Select-Object -Unique) -join " | ")
    } catch { return "couldn't read it: $($_.Exception.Message)" }
}
function StillRunning {
    $left = Leftovers
    if (-not $left.Count) { return "" }
    $texts = $left | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -ne "Recruence" } |
        ForEach-Object { "[$($_.MainWindowTitle): $(WindowText $_.MainWindowHandle)]" }
    return (Describe $left) + $(if ($texts) { " " + ($texts -join " ") } else { "" })
}
function WaitClosed([int]$seconds = 40) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) { if ((Leftovers).Count -eq 0) { return $true }; Start-Sleep -Seconds 1 }
    return $false
}

# 1. Clean machine. Anything else stops the test before it installs anything: on a machine with Recruence (or an
#    install from before the rename, data in %APPDATA%\MIA Interviewer) it would upgrade and uninstall that copy.
$formerDir = Join-Path $env:APPDATA "MIA Interviewer"
$clean = (-not (UninstallEntry)) -and (-not (Test-Path $exe))
Result "Clean VM: no Recruence installed" $clean
Result "Clean VM: no data folder" (-not (Test-Path $dataDir)) $dataDir
Result "Clean VM: no data from earlier versions" (-not (Test-Path $formerDir)) $formerDir
Result "Clean VM: Recruence not running" ((Leftovers).Count -eq 0)
if ($failures -gt 0) {
    $stop = "`nNot a clean machine: stopped before installing anything. Run this only on a fresh VM."
    Write-Host $stop; Add-Content -Encoding utf8 $log $stop
    exit 100
}
foreach ($f in @($OldInstaller, $NewInstaller)) { if (-not (Test-Path $f -PathType Leaf)) { Write-Host "Installer not found: $f"; exit 101 } }

# 2. Install the old version
$code = InstallSilently $OldInstaller
RefreshPaths
$entry = UninstallEntry
Result "Install (old) exit code" ($code -eq 0) "exit $code"
Result "Installed files" (Test-Path $exe) $exe
Result "Install folder named Recruence (fresh install)" ((Split-Path $installDir -Leaf) -eq "Recruence") $installDir
Result "Desktop shortcut" (Test-Path $desktopLink)
Result "Start menu shortcut" (Test-Path $startLink)
Result "Uninstall entry and version" ($null -ne $entry) $(if ($entry) { "$($entry.DisplayName) $($entry.DisplayVersion)" } else { "missing" })
$oldVersion = $entry.DisplayVersion

# 3. Launch the old version
Start-Process -FilePath $exe
$t = WaitBackend
Result "Old version: backend ready" ($t -ge 0) "$t s"
if ($Unattended) {
    try { SetUpThroughTheApi; Result "Old version: setup, account and role (API)" (RoleStillThere) $roleTitle }
    catch { Result "Old version: setup, account and role (API)" $false $_.Exception.Message }
    CloseApp
} else {
    Read-Host "In the Recruence window: finish the first-run setup, create an account and a role, then CLOSE Recruence. Press Enter here when it's closed"
}

# 4. Nothing left running
Result "Old version: no processes left after closing" (WaitClosed) (StillRunning)
$dbBefore = Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object Name -eq "interviewer.db" | Select-Object -First 1
Result "Data folder created" (Test-Path $dataDir) $dataDir
$hashBefore = if ($dbBefore) { (Get-FileHash $dbBefore.FullName).Hash } else { "" }

# 5. Upgrade
$code = InstallSilently $NewInstaller
RefreshPaths
$entry = UninstallEntry
Result "Upgrade exit code" ($code -eq 0) "exit $code"
Result "Version changed" ($entry -and $entry.DisplayVersion -ne $oldVersion) "$oldVersion -> $($entry.DisplayVersion)"
$dbAfter = Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object Name -eq "interviewer.db" | Select-Object -First 1
Result "Data kept by the upgrade" ($dbAfter -and ((Get-FileHash $dbAfter.FullName).Hash -eq $hashBefore)) $(if ($dbAfter) { $dbAfter.FullName } else { "database file missing" })

# 6. Launch the new version
Start-Process -FilePath $exe
$t = WaitBackend
Result "New version: backend ready" ($t -ge 0) "$t s"
try { $setup = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/setup/status" -TimeoutSec 5; Result "Setup still complete after the upgrade" ([bool]$setup.completed) } catch { Result "Setup still complete after the upgrade" $false $_.Exception.Message }
if ($Unattended) {
    Result "Still signed in, role kept (same session, API)" (RoleStillThere) $roleTitle
    CloseApp
} else {
    $answer = Read-Host "In the window: are you still signed in, and is your role there? Type y or n, then CLOSE Recruence and press Enter"
    Result "Still signed in, role kept (your answer)" ($answer -match "^(y|yes)$") $answer
}
Result "New version: no processes left after closing" (WaitClosed) (StillRunning)

# 7. Uninstall
$uninstaller = Join-Path $installDir "Uninstall Recruence.exe"
$found = Test-Path $uninstaller -PathType Leaf
Result "Uninstaller found" $found $uninstaller
if ($found) { Start-Process -FilePath $uninstaller -ArgumentList "/S" -Wait; Start-Sleep -Seconds 5 }
Result "Program files removed" (-not (Test-Path $exe))
Result "Desktop shortcut removed" (-not (Test-Path $desktopLink))
Result "Start menu shortcut removed" (-not (Test-Path $startLink))
Result "Uninstall entry removed" (-not (UninstallEntry))
Result "No processes left" ((Leftovers).Count -eq 0)
Result "Data folder kept (default answer: keep)" (Test-Path $dataDir) $dataDir

$summary = "`n$failures failure(s). Results in $log"
Write-Host $summary; Add-Content -Encoding utf8 $log $summary
exit $failures
