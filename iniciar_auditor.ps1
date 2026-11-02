$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Criando ambiente virtual local...'
    python -m venv .venv
}
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
& $python -c 'import streamlit, openpyxl, dotenv' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Instalando dependências no ambiente virtual local...'
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
exit $LASTEXITCODE
