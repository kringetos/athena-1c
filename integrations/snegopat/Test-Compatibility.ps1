param([Parameter(Mandatory=$true)][string]$Platform)
$ErrorActionPreference = 'Stop'
$taskExe = Get-Item -LiteralPath $Platform
if ($taskExe.Name -ine '1cv8.exe') { throw 'Укажите путь к 1cv8.exe.' }
$taskVersion = $taskExe.VersionInfo.FileVersion
if (!$taskVersion) { throw 'Не удалось прочитать версию платформы. Подключение не выполнялось.' }
$taskParsedVersion = [version]$taskVersion
$taskStream = [IO.File]::OpenRead($taskExe.FullName)
try {
    $taskReader = [IO.BinaryReader]::new($taskStream)
    if ($taskReader.ReadUInt16() -ne 0x5a4d) { throw 'Файл не является PE-программой.' }
    $taskStream.Position = 0x3c
    $taskHeader = $taskReader.ReadUInt32()
    $taskStream.Position = $taskHeader
    if ($taskReader.ReadUInt32() -ne 0x4550) { throw 'Некорректный PE-заголовок.' }
    $taskMachine = $taskReader.ReadUInt16()
} finally { $taskStream.Dispose() }
$taskArchitecture = switch ($taskMachine) { 0x14c {'x86'} 0x8664 {'x64'} default {'unknown'} }
# The upstream README states 8.2.19..8.3.18, not 8.5 support.
# Even an in-range version is only a candidate; this does not approve a DLL build.
$taskInRange = $taskParsedVersion -ge [version]'8.2.19.0' -and $taskParsedVersion -lt [version]'8.3.19.0'
[pscustomobject]@{
    Platform = $taskExe.FullName
    Version = $taskVersion
    Architecture = $taskArchitecture
    UpstreamDeclaredRange = '8.2.19..8.3.18; see upstream README'
    Status = $(if ($taskInRange) {'candidate_requires_matching_build'} else {'not_declared_supported'})
    LoadedAnything = $false
    Message = $(if ($taskInRange) {'Нужна проверенная сборка Снегопата для точного релиза и разрядности.'} else {'Совместимость исходного Снегопата с этой версией не заявлена. DLL не загружалась.'})
} | ConvertTo-Json -Depth 3
