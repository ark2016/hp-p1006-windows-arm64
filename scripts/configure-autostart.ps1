param(
    [ValidateSet('Enable', 'Disable', 'Status')][string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'run-bridge-background.py'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup, [Environment+SpecialFolderOption]::DoNotVerify)
if ([string]::IsNullOrWhiteSpace($startup)) { throw 'Windows did not return a Startup folder for the current user.' }
$linkPath = Join-Path $startup 'HP P1006 Bridge.lnk'
$arguments = '-X utf8 "' + $launcher + '"'
$state = Join-Path $root 'state'
$reportPath = Join-Path $state 'autostart.json'

function Test-Bridge {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8631/ipp/print' -UseBasicParsing -DisableKeepAlive -TimeoutSec 2
        return ($response.StatusCode -eq 200 -and $response.Content.StartsWith('HP LaserJet P1006 local IPP bridge.'))
    } catch { return $false }
}

try {
    # Sandbox processes can inherit the real user's APPDATA. Never install
    # persistence using that mismatched account/profile combination.
    if ($Action -ne 'Status' -and $identity -match '\\CodexSandbox[^\\]*$') {
        throw 'Run Enable-Autostart.cmd or Disable-Autostart.cmd in your normal Windows session, not the Codex sandbox.'
    }
    $shell = New-Object -ComObject WScript.Shell
    $exists = Test-Path -LiteralPath $linkPath
    $owned = $false
    if ($exists) {
        $existing = $shell.CreateShortcut($linkPath)
        $owned = ($existing.Arguments -ceq $arguments -and
            $existing.WorkingDirectory -ieq $root -and
            [IO.Path]::GetFileName($existing.TargetPath) -ieq 'pythonw.exe')
    }
    if ($Action -eq 'Status') {
        [ordered]@{
            user = $identity
            shortcut = $linkPath
            shortcut_exists = $exists
            belongs_to_this_project = $owned
            bridge_responds = (Test-Bridge)
            note = 'A shortcut does not prove a successful Windows sign-in launch. Windows Startup Apps can also disable it.'
        } | ConvertTo-Json
        exit 0
    }
    if ($exists -and -not $owned) {
        throw "A different shortcut already exists at '$linkPath'. It was not changed."
    }
    New-Item -ItemType Directory -Path $state -Force | Out-Null
    if ($Action -eq 'Disable') {
        if ($exists) { Remove-Item -LiteralPath $linkPath }
        [ordered]@{
            time_utc = [DateTime]::UtcNow.ToString('o')
            status = 'removed'
            user = $identity
            shortcut = $linkPath
            running_bridge_stopped = $false
        } | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding UTF8
        Write-Host 'Automatic startup removed. The running bridge is unchanged; it stops when you sign out.'
        exit 0
    }
    Push-Location -LiteralPath $root
    try {
        $python = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
        $resolvedPython = & $python -X utf8 -c 'import sys; import PIL; import p1006; print(sys.executable)'
        if ($LASTEXITCODE -ne 0 -or -not $resolvedPython) { throw 'Python with Pillow and p1006 could not be loaded.' }
        $pythonw = Join-Path (Split-Path -Parent ([string]$resolvedPython)) 'pythonw.exe'
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw "pythonw.exe not found: $pythonw" }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "Background launcher not found: $launcher" }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'bin/foo2xqx.exe') -PathType Leaf)) { throw 'Printer converter is missing. Run scripts/prepare.ps1 first.' }
    New-Item -ItemType Directory -Path $startup -Force | Out-Null
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $pythonw
    $link.Arguments = $arguments
    $link.WorkingDirectory = $root
    $link.Description = 'HP LaserJet P1006 local printing bridge'
    $link.Save()
    $saved = $shell.CreateShortcut($linkPath)
    if ($saved.TargetPath -ine $pythonw -or $saved.Arguments -cne $arguments -or $saved.WorkingDirectory -ine $root) {
        throw 'Startup shortcut verification failed.'
    }
    $alreadyRunning = Test-Bridge
    $startedPid = $null
    if (-not $alreadyRunning) {
        $started = Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru
        $startedPid = $started.Id
    }
    $ready = $alreadyRunning
    for ($attempt = 0; $attempt -lt 20 -and -not $ready; $attempt++) {
        Start-Sleep -Milliseconds 250
        $ready = Test-Bridge
    }
    [ordered]@{
        time_utc = [DateTime]::UtcNow.ToString('o')
        status = $(if ($ready) { 'installed' } else { 'installed_but_bridge_not_ready' })
        user = $identity
        shortcut = $linkPath
        target = $pythonw
        arguments = $arguments
        working_directory = $root
        existing_bridge_kept = $alreadyRunning
        started_process_id = $startedPid
        bridge_responds = $ready
        windows_sign_in_tested = $false
    } | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding UTF8
    if (-not $ready) { throw 'Startup installed, but the bridge did not respond. See state/background.log.' }
    Write-Host 'Automatic startup installed for this Windows user. The bridge responds.'
    Write-Host 'It will start without a terminal window at your next Windows sign-in. Keep this project folder in place.'
    if ($alreadyRunning) { Write-Host 'The existing bridge was kept. Keep its current window open until you sign out.' }
    Write-Host "Result: $reportPath"
} catch {
    [Console]::Error.WriteLine('Autostart setup failed: ' + $_.Exception.Message)
    exit 1
}
