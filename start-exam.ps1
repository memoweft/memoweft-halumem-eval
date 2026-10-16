param([int]$SessionLimit = 65, [string]$RunName = 'medium-user-01-new')
$ErrorActionPreference = 'Stop'
$examRoot = $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $examRoot 'STOP')) { throw 'STOP is present. Read README.md before starting a paid run.' }
if (-not $env:MIMO_API_KEY) { $env:MIMO_API_KEY = [Environment]::GetEnvironmentVariable('MIMO_API_KEY','User') }
if (-not $env:MIMO_API_KEY) { $env:MIMO_API_KEY = [Environment]::GetEnvironmentVariable('MIMO_API_KEY','Machine') }
if (-not $env:MIMO_API_KEY) { throw 'MIMO_API_KEY is unavailable' }
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HF_HOME = Join-Path $examRoot 'hf-cache'
$examPython = Join-Path $examRoot '.venv\Scripts\python.exe'
Set-Location -LiteralPath $examRoot
& $examPython 'runner.py' '--session-limit' $SessionLimit '--run' $RunName
exit $LASTEXITCODE
