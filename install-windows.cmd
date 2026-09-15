@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "METER_EXIT=1"
if not exist "%~dp0scripts\install.py" goto missing_files

where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 goto run_py
)
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 goto run_python
)
echo Python 3.10 or newer was not found.
echo Install Python manually, reopen the terminal, and retry.
echo See docs\SETUP.windows.zh-CN.md for instructions.
goto finish

:run_py
py -3 -X utf8 "%~dp0scripts\install.py"
set "METER_EXIT=%ERRORLEVEL%"
goto finish

:run_python
python -X utf8 "%~dp0scripts\install.py"
set "METER_EXIT=%ERRORLEVEL%"
goto finish

:missing_files
echo The source folder is incomplete. Extract the full archive first.

:finish
echo.
if not "%METER_EXIT%"=="0" echo Setup did not complete. Review the message above before retrying.
if not defined CI pause
exit /b %METER_EXIT%
