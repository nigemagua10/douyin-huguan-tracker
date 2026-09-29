@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
python "%~dp0src\main.py" %*
echo.
pause
