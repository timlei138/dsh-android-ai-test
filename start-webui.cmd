@echo off
REM Launch the case WebUI. Real logic lives in start-webui.ps1.
REM
REM Why a .cmd wrapper: double-clicking a .ps1 opens it in Notepad instead of
REM running it. A .cmd runs when double-clicked.
REM
REM NOTE: keep this file pure ASCII. cmd.exe reads .cmd as ANSI and Chinese
REM text gets mangled into commands (this bit us once already).

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-webui.ps1" %*
if errorlevel 1 pause
