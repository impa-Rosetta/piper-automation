@echo off
setlocal
cd /d "%~dp0"
title Piper Windows Remote Workbench

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found in PATH.
  echo Install Python 3.10 or newer, then run this file again.
  pause
  exit /b 1
)

where ssh >nul 2>nul
if errorlevel 1 (
  echo Windows OpenSSH Client was not found.
  echo Enable "OpenSSH Client" in Windows Optional Features.
  pause
  exit /b 1
)

python -X utf8 -m teach.windows_remote_workbench
if errorlevel 1 pause
