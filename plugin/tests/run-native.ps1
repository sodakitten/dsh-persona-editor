param([Parameter(Mandatory=$true)][string]$DshDirectory)
$ErrorActionPreference = 'Stop'
$executable = Join-Path $DshDirectory 'DeepSeek Harness.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'DSH executable not found' }
$testScript = Join-Path $PSScriptRoot 'native-smoke.mjs'
$testOutput = Join-Path ([IO.Path]::GetTempPath()) ('dsh-preset-check-' + [guid]::NewGuid().ToString('N'))
$previousNodeMode = $env:ELECTRON_RUN_AS_NODE
try {
    $env:ELECTRON_RUN_AS_NODE = '1'
    $arguments = '--expose-internals "' + $testScript + '" "' + (Resolve-Path -LiteralPath $DshDirectory).Path.TrimEnd('\') + '"'
    $process = Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait `
        -RedirectStandardOutput ($testOutput + '.out') -RedirectStandardError ($testOutput + '.err')
    Get-Content -LiteralPath ($testOutput + '.out') -Encoding UTF8
    Get-Content -LiteralPath ($testOutput + '.err') -Encoding UTF8
    if ($process.ExitCode -ne 0) { throw "Native check failed: $($process.ExitCode)" }
} finally {
    $env:ELECTRON_RUN_AS_NODE = $previousNodeMode
    foreach ($extension in '.out', '.err') {
        $testLog = $testOutput + $extension
        if (Test-Path -LiteralPath $testLog) { Remove-Item -LiteralPath $testLog }
    }
}
