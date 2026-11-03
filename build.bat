@echo off
cd /d "%~dp0"
echo Building DiscordMusicRPC.exe...
python -m PyInstaller --onefile --noconfirm --clean --name "DiscordMusicRPC" --paths src --collect-all winsdk --hidden-import requests --hidden-import pypresence src/main.py
if errorlevel 1 (
    echo [ERROR] Build failed!
    pause
    exit /b 1
)
echo.
echo [OK] Build completed: dist\DiscordMusicRPC.exe
pause
