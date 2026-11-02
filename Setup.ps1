$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create virtual environment. Install Python 3.12+ (recommended 3.13) first.' }
}
python -m pip --python .venv\Scripts\python.exe install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .venv\Scripts\python.exe tools\download_ocr_models.py
if ($LASTEXITCODE -ne 0) { throw 'OCR model download or checksum verification failed.' }
Write-Host 'Ready. Run .venv\Scripts\python.exe main.py.'
