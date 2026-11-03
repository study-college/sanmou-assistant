@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist "run_assistant.py" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-source.ps1"
  if errorlevel 1 pause
  exit /b
)
if exist "SanmouAssistant.exe" (
  if exist "%~dp0..\release-manifest.json" (
    start "" "%~dp0SanmouAssistant.exe" --data-dir "%~dp0..\data"
  ) else (
    start "" "%~dp0SanmouAssistant.exe" --data-dir "%~dp0data"
  )
  exit /b
)
echo Please extract the complete package and use its root launcher.
pause
