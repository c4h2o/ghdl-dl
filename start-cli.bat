@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopy
%PY% "%~dp0run_cli.py" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
:nopy
echo 未找到 Python。请安装 Python 3.9+ 并勾选 "Add Python to PATH"。
pause
exit /b 1
