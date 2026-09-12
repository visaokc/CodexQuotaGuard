$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot 'work\venv\Scripts\python.exe'
$dist = Join-Path $PSScriptRoot 'dist\0.6.7'
if (-not (Test-Path -LiteralPath $python)) { throw '先按 README 创建 work\venv 并安装 requirements-dev.txt' }
& "$PSScriptRoot\scripts\build_tsnet.ps1"
& $python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& npm.cmd --prefix frontend ci
if ($LASTEXITCODE -ne 0) { throw 'Frontend install failed' }
& npm.cmd --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
& $python scripts\prepare_assets.py
if ($LASTEXITCODE -ne 0) { throw 'Asset/license preparation failed' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name CodexQuotaUpdater --exclude-module tkinter --exclude-module PIL --exclude-module aiortc --exclude-module av --distpath work\updater --workpath work\build-updater updater_main.py
if ($LASTEXITCODE -ne 0) { throw 'Updater build failed' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name 'Codex配额管家' --icon assets\app.ico --collect-all webview --hidden-import webview.platforms.edgechromium --hidden-import webview.platforms.winforms --hidden-import clr --hidden-import pythonnet --collect-all clr_loader --exclude-module tkinter --exclude-module customtkinter --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 --exclude-module gi --exclude-module cefpython3 --hidden-import pystray._win32 --exclude-module quota_guard.mesh --exclude-module quota_guard.file_mesh --exclude-module aiortc --exclude-module av --exclude-module pylibsrtp --exclude-module PIL._avif --exclude-module PIL._webp --exclude-module PIL._imagingft --add-binary 'vendor\tsnet\cqg-tsnet.exe;vendor\tsnet' --add-binary 'work\updater\CodexQuotaUpdater.exe;vendor' --add-data 'licenses;licenses' --add-data 'assets/app.ico;assets' --add-data 'frontend/dist;frontend/dist' --add-data 'vendor\tsnet\manifest.json;vendor\tsnet' --add-data 'vendor\tsnet\LICENSE.txt;licenses\tsnet' --add-data 'vendor\tsnet\THIRD_PARTY_LICENSES.txt;licenses\tsnet' --distpath $dist --workpath work\build-client main.py
if ($LASTEXITCODE -ne 0) { throw 'Client build failed' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name '恢复Codex网络' --icon assets\app.ico --distpath $dist --workpath work\build-recovery recover_main.py
if ($LASTEXITCODE -ne 0) { throw 'Recovery build failed' }
Copy-Item -LiteralPath README.md,VALIDATION.md,AGENTS.md,THIRD_PARTY_NOTICES.txt -Destination $dist -Force
Copy-Item -LiteralPath licenses -Destination $dist -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'licenses\tsnet') | Out-Null
Copy-Item -LiteralPath vendor\tsnet\LICENSE.txt,vendor\tsnet\THIRD_PARTY_LICENSES.txt -Destination (Join-Path $dist 'licenses\tsnet') -Force
Copy-Item -LiteralPath assets,docs -Destination $dist -Recurse -Force
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $dist 'Codex配额管家.exe'),(Join-Path $dist '恢复Codex网络.exe') | Format-Table -AutoSize
