@echo off
rem openLight first-time setup: create .venv and install dependencies.
rem (Messages are ASCII on purpose: cmd.exe mangles UTF-8 batch files.)
setlocal
cd /d "%~dp0"

set "MIRROR=https://mirrors.huaweicloud.com/repository/pypi/simple/"
set "VENV_PY=%CD%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto :install

set "BOOTSTRAP="
where py >nul 2>nul && set "BOOTSTRAP=py -3"
if not defined BOOTSTRAP (
    where python >nul 2>nul && set "BOOTSTRAP=python"
)
if not defined BOOTSTRAP (
    echo.
    echo   Python was not found. Please install Python 3.10+ first:
    echo       https://www.python.org/downloads/windows/
    echo   Remember to tick "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [1/2] Creating virtual environment .venv ...
%BOOTSTRAP% -m venv "%CD%\.venv"
if not exist "%VENV_PY%" (
    echo.
    echo   Failed to create .venv. Please install the official Python from python.org and retry.
    echo.
    pause
    exit /b 1
)

:install
echo [2/2] Installing dependencies (PySide6 / Pillow / numpy) ...
"%VENV_PY%" -m pip install --index-url %MIRROR% -r "%CD%\requirements.txt"
if errorlevel 1 (
    echo.
    echo   Installation failed. Check your network connection and retry.
    echo.
    pause
    exit /b 1
)

echo.
echo   Done. Double-click start.cmd to launch openLight.
echo.
pause
endlocal
