@echo off
cd /d "%~dp0"
echo This replaces the AI API key saved for this bot (the provider set in config.json), then restarts the control room.
choice /m "Continue"
if errorlevel 2 exit /b
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=(Get-Content -Raw config.json | ConvertFrom-Json).ai_provider; if($p -eq 'openai'){$f='openai.dpapi'}else{$f='gemini.dpapi'}; Remove-Item -LiteralPath (Join-Path '.secrets' $f) -ErrorAction SilentlyContinue"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0friend_start.ps1"
if errorlevel 1 pause
