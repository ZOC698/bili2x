param([switch]$Gpu)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& py -3.11 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 environment creation failed.' }
$requirements = if ($Gpu) { 'requirements-gpu.txt' } else { 'requirements.txt' }
& .\.venv\Scripts\python.exe -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Environment ready. Configure your API key before starting.'
