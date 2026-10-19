@echo off
REM ===========================================================================
REM  RUN_PROBE.bat  -  double-click this on the office server.
REM
REM  It runs office_probe.ps1 in the same folder, pauses so you can read the
REM  result, and leaves a .txt report you can email back.
REM  No Python, no installs, no admin rights needed.
REM ===========================================================================
title Innotech / NeoView network probe
cd /d "%~dp0"

echo.
echo Running network probe, please wait...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0office_probe.ps1" %*

echo.
echo ===========================================================================
echo Finished. Send back the office_probe_*.txt file from this folder.
echo Press any key to close.
echo ===========================================================================
pause >nul
