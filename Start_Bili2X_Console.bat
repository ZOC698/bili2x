@echo off
chcp 65001 >nul
title Bili2X
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run Setup_Bili2X.ps1 first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0bili2x_watcher.py"
pause
