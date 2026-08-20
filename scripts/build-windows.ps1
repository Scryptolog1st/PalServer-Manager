$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
python -m pip install -e ".[all,build]"
pyinstaller --noconfirm --clean --windowed --name PalServerManager --collect-all PySide6 --collect-data palserver_manager --hidden-import palserver_manager.gui scripts\gui_entry.py
pyinstaller --noconfirm --clean --console --name PalServerAgent --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on --collect-submodules fastapi scripts\agent_entry.py
Write-Host "Builds are in dist\PalServerManager and dist\PalServerAgent"
