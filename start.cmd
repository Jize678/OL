@echo off
rem openLight launcher. Double-click this file to start the app.
rem (Messages are ASCII on purpose: cmd.exe mangles UTF-8 batch files.)
setlocal
cd /d "%~dp0"

if exist "%CD%\.venv\Scripts\pythonw.exe" (
    start "" "%CD%\.venv\Scripts\pythonw.exe" "%CD%\run_gui.py" %*
    goto :end
)

if exist "%CD%\.venv\Scripts\python.exe" (
    "%CD%\.venv\Scripts\python.exe" "%CD%\run_gui.py" %*
    goto :end
)

echo.
echo   openLight is not set up yet.
echo   Please run setup.cmd first (it creates .venv and installs dependencies).
echo.
pause

:end
endlocal
