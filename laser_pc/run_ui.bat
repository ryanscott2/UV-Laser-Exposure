@echo off
setlocal enableextensions enabledelayedexpansion
REM ---------------------------------------------------------------------------
REM Double-click to open the UV Laser Exposure UI (no console window).
REM
REM Reuses an existing venv (Tkinter + pywin32 + pyserial -- the same deps the
REM Singulation code uses). Search order for pythonw.exe:
REM   1. .\venv                      a venv created in this folder
REM   2. ..\venv  and  ..\..\venv    a SHARED venv one/two levels up, e.g. the
REM                                  Singulation venv at ...\GoodsonGroup\Ryan\venv
REM   3. the full path on the first line of venv_path.txt next to this file
REM   4. pythonw.exe on PATH, then the "py" launcher
REM ---------------------------------------------------------------------------

set "UI=%~dp0expose_ui.py"

REM 1-2: a venv here, or a shared one one/two directories up.
for %%D in ("%~dp0venv" "%~dp0..\venv" "%~dp0..\..\venv") do (
    if exist "%%~fD\Scripts\pythonw.exe" (
        set "PYW=%%~fD\Scripts\pythonw.exe"
        goto launch
    )
)

REM 3: explicit absolute override (bulletproof if the layout ever changes).
if exist "%~dp0venv_path.txt" (
    set /p PYW=<"%~dp0venv_path.txt"
    if exist "!PYW!" goto launch
)

REM 4: any pythonw on PATH, else the py launcher.
for %%P in (pythonw.exe) do if not "%%~$PATH:P"=="" (
    set "PYW=%%~$PATH:P"
    goto launch
)
where py.exe >nul 2>nul
if not errorlevel 1 (
    start "" py -w "%UI%"
    goto done
)

echo(
echo Could not find a pythonw.exe to run the UI.
echo Reuse the Singulation venv: put its pythonw path, e.g.
echo   C:\Users\samurai\Desktop\UserJobs\GoodsonGroup\Ryan\venv\Scripts\pythonw.exe
echo on the first line of a file named venv_path.txt next to this run_ui.bat,
echo or create one here with:  python -m venv venv
echo(
pause
goto done

:launch
start "" "!PYW!" "%UI%"

:done
endlocal
