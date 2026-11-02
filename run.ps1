# Start OCI Monitor on Windows. Creates .venv on first run.
#
#   .\run.ps1 [flags]           run in the foreground (Ctrl+C to stop)
#   .\run.ps1 start [flags]     run in the background, logging to ocimon.log / ocimon.err.log
#   .\run.ps1 stop | status | logs
#
# Flags go to `python -m ocimon`, e.g. .\run.ps1 start --regions all
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$pidFile = Join-Path $PSScriptRoot ".ocimon.pid"
$logFile = Join-Path $PSScriptRoot "ocimon.log"
$errFile = Join-Path $PSScriptRoot "ocimon.err.log"

function Ensure-Venv {
    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating virtual environment in .venv ..."
        $python = if ($env:PYTHON) { $env:PYTHON } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
        & $python -m venv .venv
        & $venvPython -m pip install --quiet --upgrade pip
        & $venvPython -m pip install --quiet -r requirements.txt
    }
}

function Get-Running {
    if (-not (Test-Path $pidFile)) { return $null }
    Get-Process -Id ([int](Get-Content $pidFile)) -ErrorAction SilentlyContinue
}

$command = if ($args.Count -gt 0) { $args[0] } else { "" }
$rest = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

switch ($command) {
    "start" {
        Ensure-Venv
        if (Get-Running) { Write-Host "OCI Monitor is already running (pid $(Get-Content $pidFile))"; exit 0 }
        $proc = Start-Process -FilePath $venvPython -ArgumentList (@("-m", "ocimon") + $rest) -WindowStyle Hidden `
            -RedirectStandardOutput $logFile -RedirectStandardError $errFile -PassThru
        Set-Content -Path $pidFile -Value $proc.Id
        Start-Sleep -Seconds 3
        if ($proc.HasExited) {
            Remove-Item $pidFile -ErrorAction SilentlyContinue
            Write-Host "OCI Monitor failed to start:"; Get-Content $errFile -Tail 5; exit 1
        }
        Write-Host "OCI Monitor is running in the background (pid $($proc.Id)). Logs: .\run.ps1 logs   Stop: .\run.ps1 stop"
    }
    "stop" {
        $p = Get-Running
        if ($p) { Stop-Process -Id $p.Id; Write-Host "OCI Monitor stopped" } else { Write-Host "OCI Monitor is not running" }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
    "status" {
        if (Get-Running) { Write-Host "OCI Monitor is running (pid $(Get-Content $pidFile))" } else { Write-Host "OCI Monitor is not running"; exit 3 }
    }
    "logs" { Get-Content $errFile -Tail 50 -Wait }
    default {
        Ensure-Venv
        & $venvPython -m ocimon @args
        exit $LASTEXITCODE
    }
}
