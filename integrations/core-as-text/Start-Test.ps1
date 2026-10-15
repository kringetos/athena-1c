param([string]$Platform)
$ErrorActionPreference = 'Stop'
$taskWorkspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$taskRoot = Join-Path $taskWorkspace '.athena\integrations\test-8.5'
$taskRuntime = Join-Path $taskRoot 'text-runtime'
$taskBase = Join-Path $taskRoot 'empty-base'
if (!$Platform) {
    $taskSettings = Get-Content -LiteralPath (Join-Path $taskWorkspace '.athena\user.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $Platform = $taskSettings.platform
}
$taskExe = Get-Item -LiteralPath $Platform
if ($taskExe.Name -ine '1cv8.exe' -or $taskExe.VersionInfo.FileVersion -ne '8.5.1.1522') {
    throw 'This prototype was tested only on 1cv8.exe 8.5.1.1522 x86.'
}
$taskBytes = [IO.File]::ReadAllBytes($taskExe.FullName)
$taskPE = [BitConverter]::ToInt32($taskBytes, 0x3c)
if ([BitConverter]::ToUInt16($taskBytes, $taskPE + 4) -ne 0x14c) {
    throw 'This test requires the 32-bit platform.'
}
if (!(Test-Path -LiteralPath (Join-Path $taskBase '1Cv8.1CD')) -or
    !(Test-Path -LiteralPath (Join-Path $taskRuntime 'starter.exe')) -or
    !(Test-Path -LiteralPath (Join-Path $taskRuntime 'modules\AthenaText\module.info'))) {
    throw 'Prepare text-runtime and the separate empty test base first; see README.md.'
}
if (Get-Process -Name '1cv8*','starter' -ErrorAction SilentlyContinue) {
    throw 'Close other 1C and Core.As sessions before this isolated test.'
}
$taskStarter = $null
try {
    $taskStarter = Start-Process -FilePath (Join-Path $taskRuntime 'starter.exe') -WorkingDirectory $taskRuntime -WindowStyle Hidden -PassThru
    $taskDesigner = Start-Process -FilePath $taskExe.FullName -ArgumentList ('DESIGNER /F "' + $taskBase + '"') -WindowStyle Normal -PassThru
    $taskStartTime = $taskDesigner.StartTime
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(15)
    $taskAttached = $false
    while ([DateTime]::UtcNow -lt $taskDeadline) {
        $taskDesigner.Refresh()
        if ($taskDesigner.HasExited) { throw 'The test Designer exited; inspect the runtime logs.' }
        # Windows PowerShell 5 can omit 32-bit DLLs from Process.Modules.
        # Read the newly created module log instead of relying on that list.
        $taskLogs = Get-ChildItem -LiteralPath (Join-Path $taskRuntime 'logs\AthenaText') -Recurse -File -Filter '*.log' -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -ge $taskStartTime }
        if ($taskLogs | Where-Object { (Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8) -match 'AthenaText' }) {
            $taskAttached = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (!$taskAttached) { throw 'Core.As did not attach within 15 seconds; inspect the runtime logs.' }
    Start-Sleep -Seconds 2
    Write-Output ('Test Designer PID: ' + $taskDesigner.Id)
    Write-Output ('Runtime logs: ' + (Join-Path $taskRuntime 'logs'))
} finally {
    # Stop our watcher so later Designer sessions are not injected.
    if ($taskStarter) {
        $taskStarter.Refresh()
        if (!$taskStarter.HasExited) { Stop-Process -Id $taskStarter.Id }
    }
}
