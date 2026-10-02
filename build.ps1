# Rebuild both exes with PyInstaller.
# Usage: powershell -File build.ps1      (Windows PowerShell 5.1 works too: UTF-8 BOM)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

Write-Host 'Checking dependencies...'
& python -c "import sys, tkinter; print('python', sys.version.split()[0])"
& python -m PyInstaller --version

Write-Host 'Building the windowed exe...'
& python -m PyInstaller --onefile --noconsole --noconfirm --name '人设编辑' `
    --distpath dist --workpath build --specpath build persona_editor.py
if ($LASTEXITCODE -ne 0) { throw 'windowed build failed' }

Write-Host 'Building the console exe...'
& python -m PyInstaller --onefile --console --noconfirm --name '人设编辑-cli' `
    --distpath dist --workpath build --specpath build persona_editor.py
if ($LASTEXITCODE -ne 0) { throw 'console build failed' }

Write-Host ''
Write-Host 'Smoke test:'
& ".\dist\人设编辑-cli.exe" --check
if ($LASTEXITCODE -ne 0) { throw 'smoke test failed' }

Get-ChildItem dist -Filter *.exe | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }, LastWriteTime | Format-Table -AutoSize
