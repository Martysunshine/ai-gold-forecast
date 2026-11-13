@echo off
rem Windows launcher. Runs start.ps1 whatever the PowerShell script policy is.
rem   start.cmd              -> http://localhost:8742
rem   start.cmd -Port 9000   -> another port
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
