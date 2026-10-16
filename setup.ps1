$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
Get-Command git,uv -ErrorAction Stop | Out-Null
git submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { throw 'Submodule checkout failed' }
uv venv --python 3.12 '.venv'
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
uv pip install --python '.venv\Scripts\python.exe' -r 'requirements-lock.txt'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Write-Output 'Environment ready. No model requests have been made.'
