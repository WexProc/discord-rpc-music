@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo.
echo ======================================================
echo Готово! Запускай start.bat или build.bat для сборки .exe
echo ======================================================
pause
