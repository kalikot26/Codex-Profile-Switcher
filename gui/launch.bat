@echo off
REM Launch Kalikot Profile Switcher GUI (no console window).
setlocal
set HERE=%~dp0
start "" pythonw "%HERE%app.py"
