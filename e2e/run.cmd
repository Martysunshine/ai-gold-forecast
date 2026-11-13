@echo off
rem Windows runner for the browser checks. Runs run.ps1 whatever the PowerShell script policy is.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
