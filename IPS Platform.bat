@echo off
title IPS Platform — Indoor Positioning System
echo.
echo  ===================================
echo   IPS Platform - Starting up...
echo  ===================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python and add it to PATH.
    echo         https://python.org/downloads
    echo         Make sure to check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

:: Check pywebview is installed
python -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo [SETUP] Installing desktop app dependencies...
    pip install pywebview requests
)

:: Run the desktop app
cd /d "%~dp0"
python desktop_app.py
