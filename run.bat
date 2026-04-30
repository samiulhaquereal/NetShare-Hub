@echo off
title ScreenShare
python "%~dp0server.py"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to start. Run install.bat first to install dependencies.
    pause
)
