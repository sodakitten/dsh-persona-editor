# Rebuild both exes with PyInstaller.
# Usage: powershell -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# The Chinese exe names live in build-names.json (UTF-8) instead of in this
# script on purpose: Windows PowerShell 5.1 reads a BOM-less script as ANSI,
# which silently mangles such literals into garbage file names.
$names = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'build-names.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$guiExe = $names.gui
$cliExe = $names.cli

Write-Host 'Checking dependencies...'
& python -c "import sys, tkinter; print('python', sys.version.split()[0])"
& python -m PyInstaller --version

# Rewriting session records needs zstd. Prefer the `zstandard` package: its C
# backend is a distinct module name, so freezing it is reliable.
$zstdArgs = @()
& python -c "import zstandard" 2>$null
if ($LASTEXITCODE -eq 0) {
    $zstdArgs = @('--collect-all', 'zstandard')
    Write-Host 'zstd support will be bundled (zstandard).'
} else {
    Write-Host 'zstandard is not installed: the exe will not rewrite session records.'
}

Write-Host 'Building the windowed exe...'
& python -m PyInstaller --onefile --noconsole --noconfirm --name $names.gui_stem `
    --distpath dist --workpath build --specpath build @zstdArgs persona_editor.py
if ($LASTEXITCODE -ne 0) { throw 'windowed build failed' }

Write-Host 'Building the console exe...'
& python -m PyInstaller --onefile --console --noconfirm --name $names.cli_stem `
    --distpath dist --workpath build --specpath build @zstdArgs persona_editor.py
if ($LASTEXITCODE -ne 0) { throw 'console build failed' }

Write-Host ''
Write-Host 'Smoke test:'
& (Join-Path 'dist' $cliExe) --check
if ($LASTEXITCODE -ne 0) { throw 'smoke test failed' }

Get-ChildItem dist -Filter *.exe | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }, LastWriteTime | Format-Table -AutoSize
