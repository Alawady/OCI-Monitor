# Start OCI Monitor on Windows. Creates .venv on first run. All arguments go to `python -m ocimon`.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment in .venv ..."
    $python = if ($env:PYTHON) { $env:PYTHON } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
    & $python -m venv .venv
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -r requirements.txt
}

& $venvPython -m ocimon @args
exit $LASTEXITCODE
