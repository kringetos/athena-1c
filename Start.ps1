$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Start-Athena([string]$taskPython, [string[]]$taskPrefix = @()) {
    & $taskPython @taskPrefix -c "import importlib.metadata as m; import antlr4, numpy, onnxruntime, tokenizers, huggingface_hub; assert m.version('antlr4-python3-runtime') == '4.13.2'; assert m.version('tokenizers') == '0.23.3'; assert m.version('huggingface-hub') == '1.33.0'" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Installing Athena dependencies...'
        & $taskPython @taskPrefix -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $taskPython @taskPrefix -m athena.server
    exit $LASTEXITCODE
}
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $projectPython) {
    Start-Athena $projectPython
}
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) {
    Start-Athena $bundledPython
}
$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pythonLauncher) {
    & $pythonLauncher.Source -3 -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Start-Athena $pythonLauncher.Source @('-3')
    }
}
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) {
    & $pythonCommand.Source -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Start-Athena $pythonCommand.Source
    }
}
Write-Host 'Python 3.10+ is required. Install Python with tkinter, then run Start.cmd again.'
exit 1
