@echo off
title ScreenShare — Setup & Launch
color 0A
echo.
echo  ============================================
echo        ^|^|^|  ScreenShare Setup  ^|^|^|
echo  ============================================
echo.
echo  [1/2] Installing Python dependencies...
echo.
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] pip install failed. Make sure Python is installed.
    echo  Download Python from: https://python.org/downloads
    pause
    exit /b 1
)
echo.
echo  [2/2] Launching ScreenShare...
echo.
python server.py
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] Failed to start. Check the error above.
    pause
)
