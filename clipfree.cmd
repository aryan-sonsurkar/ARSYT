@echo off
setlocal
title ClipFree Studio
cd /d "%~dp0"
set CLIPFREE_RENDER_CONCURRENCY=1

echo.
echo   ================================
echo    ClipFree Studio - local server
echo   ================================
echo.
echo   Starting Ollama, backend and frontend...
echo   The studio opens at http://localhost:3000
echo.
echo   Keep this window open.
echo   Close this window or press Q to stop all ClipFree services.
echo.

where ollama.exe >nul 2>&1
if %errorlevel%==0 (set "OLLAMA_CMD=ollama.exe") else (set "OLLAMA_CMD=%LOCALAPPDATA%\Programs\Ollama\ollama.exe")

start "ollama" /b "%OLLAMA_CMD%" serve
start "clipfree-backend" /b "%~dp0.venv\Scripts\python.exe" -m backend.main
pushd frontend
start "clipfree-frontend" /b cmd /c npm run start
popd

timeout /t 10 /nobreak >nul
start "" http://localhost:3000

choice /c Q /n /m "   Press Q to stop ClipFree (or just close this window)"
if errorlevel 1 goto stop
:stop
echo.
echo   Stopping ClipFree services...
taskkill /f /im ollama.exe >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'backend\.main' -or $_.CommandLine -match 'next.*start' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo   Done.
timeout /t 2 /nobreak >nul
exit
