$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$candidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
foreach ($taskPython in $candidates) {
    if (Test-Path -LiteralPath $taskPython) {
        & $taskPython -m athena.reset_data
        exit $LASTEXITCODE
    }
}
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m athena.reset_data
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m athena.reset_data
    exit $LASTEXITCODE
}
Write-Host 'Python 3.10+ is required.'
exit 1
