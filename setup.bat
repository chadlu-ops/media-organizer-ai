@echo off
setlocal enabledelayedexpansion
title Media Organizer Setup

echo ========================================
echo   Media Organizer: Initial Deployment
echo ========================================
echo.

:: 1. Check for Python
echo [1/4] Checking for Python installation...
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: Python is not installed or not in your PATH.
    echo Please install Python 3.10+ from python.org and try again.
    pause
    exit /b 1
)
echo Python detected.

:: 2. Create Virtual Environment
echo [2/4] Initializing Virtual Environment (.venv)...
if not exist ".venv" (
    python -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
) else (
    echo Virtual environment already exists. Skipping.
)

:: 3. Upgrade Pip
echo [3/4] Upgrading Pip...
.venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
echo Pip upgraded.

:: 4. Install Dependencies
echo [4/4] Installing application dependencies...
echo This may take a few minutes depending on your internet speed.
echo.
.venv\Scripts\python.exe -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Some dependencies failed to install.
    echo Check your internet connection or the error messages above.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   SETUP COMPLETE
echo ========================================
echo.
echo You can now launch the server using 'restart_server.bat'.
echo.
pause
endlocal
