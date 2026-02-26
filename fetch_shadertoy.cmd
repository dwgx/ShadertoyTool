@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=python"

where %PYTHON_EXE% >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    exit /b 1
)

%PYTHON_EXE% -c "import requests, browser_cookie3, playwright" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing required packages: requests, browser-cookie3, playwright
    %PYTHON_EXE% -m pip install --user --disable-pip-version-check requests browser-cookie3 playwright
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        exit /b 1
    )
)

%PYTHON_EXE% "%SCRIPT_DIR%fetch_shadertoy.py" %*
set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
