$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot 'work\venv\Scripts\python.exe'
$dist = Join-Path $PSScriptRoot 'dist\0.2.6'
if (-not (Test-Path -LiteralPath $python)) { throw '先按 README 创建 work\venv 并安装 requirements-dev.txt' }
& $python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& $python scripts\prepare_assets.py
if ($LASTEXITCODE -ne 0) { throw 'Asset/license preparation failed' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name CodexQuotaUpdater --distpath work\updater --workpath work\build-updater updater_main.py
if ($LASTEXITCODE -ne 0) { throw 'Updater build failed' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name 'Codex配额管家' --icon assets\app.ico --collect-all customtkinter --collect-all aiortc --hidden-import aioice --hidden-import pystray._win32 --add-binary 'vendor\syncthing\syncthing.exe;vendor\syncthing' --add-binary 'work\updater\CodexQuotaUpdater.exe;vendor' --add-data 'licenses;licenses' --add-data 'vendor\syncthing\LICENSE.txt;licenses' --add-data 'vendor\syncthing\BUILD.md;licenses' --distpath $dist --workpath work\build-client main.py
if ($LASTEXITCODE -ne 0) { throw 'Client build failed' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name '恢复Codex网络' --icon assets\app.ico --distpath $dist --workpath work\build-recovery recover_main.py
if ($LASTEXITCODE -ne 0) { throw 'Recovery build failed' }
& $python -m PyInstaller --noconfirm --clean --onefile --console --name CodexQuotaRelay --icon assets\app.ico --distpath $dist --workpath work\build-relay server_main.py
if ($LASTEXITCODE -ne 0) { throw 'Relay build failed' }
Copy-Item -LiteralPath README.md,VALIDATION.md,THIRD_PARTY_NOTICES.txt -Destination $dist -Force
Copy-Item -LiteralPath deploy -Destination $dist -Recurse -Force
Copy-Item -LiteralPath licenses -Destination $dist -Recurse -Force
Copy-Item -LiteralPath assets -Destination $dist -Recurse -Force
Copy-Item -LiteralPath vendor\syncthing -Destination (Join-Path $dist 'syncthing-source-notices') -Recurse -Force
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $dist 'Codex配额管家.exe'),(Join-Path $dist '恢复Codex网络.exe'),(Join-Path $dist 'CodexQuotaRelay.exe') | Format-Table -AutoSize
