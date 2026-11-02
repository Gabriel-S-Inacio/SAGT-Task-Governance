@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente virtual local...
  python -m venv .venv || exit /b 1
)
set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
".venv\Scripts\python.exe" -c "import streamlit, openpyxl, dotenv" >nul 2>nul
if errorlevel 1 (
  echo Instalando dependencias no ambiente virtual local...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
exit /b %ERRORLEVEL%
