@echo off
title arb-scanner
cd /d %~dp0
rem If the scanner ever stops by itself, it is started again after 30 seconds.
rem To stop it for good: Ctrl+C, then answer Y to "Terminate batch job (Y/N)?".
:loop
venv\Scripts\python.exe scanner.py
echo.
echo %date% %time%  scanner stopped. Restarting in 30 seconds. To stop: Ctrl+C, then Y.
timeout /t 30 /nobreak >nul
goto loop
