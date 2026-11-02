@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Ambiente virtual ausente. Execute: python -m venv .venv 1>&2
  exit /b 1
)
set "PYTHONPATH=%PROJECT_ROOT%src"
set "PYTHONUTF8=1"
"%PYTHON_EXE%" -m bitrix24_task_auditor %*
exit /b %ERRORLEVEL%
