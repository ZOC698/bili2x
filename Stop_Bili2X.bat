@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop_Bili2X.ps1"
if errorlevel 1 exit /b 1
