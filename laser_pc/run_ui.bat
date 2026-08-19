@echo off
REM Double-click to open the UV Laser Exposure UI. Uses the venv's pythonw (no console).
REM The venv lives in this folder (.\venv), created during setup.
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0expose_ui.py"
