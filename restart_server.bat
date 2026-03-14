@echo off
setlocal
title Media Visualizer Helper

echo ========================================
echo   Restarting Media Visualizer Server
echo ========================================

:: Find and kill any process listening on port 8000
echo Checking for existing server on port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    echo Killing existing process PID: %%a
    taskkill /f /pid %%a >nul 2>&1
)

:: Wait a moment for port release
timeout /t 1 /nobreak >nul

echo Starting server...
echo.
python visualize_helper.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Server failed to start. 
    echo Make sure you are in the correct directory and 'python' is in your PATH.
    pause
)

endlocal
