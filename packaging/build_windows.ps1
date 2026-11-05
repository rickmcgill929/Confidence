# Build Confidence for Windows:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Produces dist\Confidence\Confidence.exe and dist\Confidence-<version>-Windows.zip
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$venv = ".build-venv"
if (-not (Test-Path $venv)) { py -3 -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$venv\Scripts\python.exe" -m pip install --quiet ".[clock]" "pyinstaller>=6.16"

& "$venv\Scripts\python.exe" packaging\stamp_build.py | Out-Host
Remove-Item -Recurse -Force build, "dist\Confidence" -ErrorAction SilentlyContinue
& "$venv\Scripts\pyinstaller.exe" --noconfirm --clean --log-level WARN packaging\dsan_clock.spec

$env:QT_QPA_PLATFORM = "offscreen"
& "dist\Confidence\Confidence.exe" --smoke-test | Out-Host
if ($LASTEXITCODE -ne 0) { throw "smoke test failed" }
Remove-Item Env:QT_QPA_PLATFORM

$version = & "$venv\Scripts\python.exe" -c "import dsan; print(dsan.__version__)"
$zip = "dist\Confidence-$version-Windows.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path "dist\Confidence" -DestinationPath $zip
& "$venv\Scripts\python.exe" packaging\stamp_build.py --clean
Write-Host "Built: dist\Confidence\Confidence.exe and $zip"
