@echo off
setlocal
cd /d "%~dp0"
set "PY=C:\Users\huawei\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" app.py
if errorlevel 1 pause