@echo off
setlocal

REM Always run from the folder this .bat lives in, even if the
REM user double-clicks from Explorer or runs from a shortcut.
cd /d "%~dp0"

REM UTF-8 so box-drawing characters in the log render correctly
REM instead of being mangled by the default Windows codepage.
chcp 65001 >nul

REM Activate a virtual environment if one is present.
if exist ".venv\Scripts\activate.bat" (
    echo Activating .venv...
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    echo Activating venv...
    call "venv\Scripts\activate.bat"
)

echo.
echo Python: 
python --version
echo.

REM -u forces unbuffered output. Without it, log lines can sit
REM in a buffer for seconds when the console is piped or captured.
python -u -m app.main

REM If Python exited non-zero, hold the window open so you can
REM read the traceback instead of it vanishing in a flash.
if errorlevel 1 (
    echo.
    echo ============================================
    echo   Bot exited with error code %errorlevel%
    echo ============================================
    pause
)

endlocal