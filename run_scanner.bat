@echo off
title arb-scanner
cd /d %~dp0
venv\Scripts\python.exe scanner.py
pause
