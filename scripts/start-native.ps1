param([ValidateSet('mock','ollama')][string]$Mode = 'mock')
$ErrorActionPreference = 'Stop'
$taskNativeRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../work/devdocqa-build'))
$taskPython = Join-Path $taskNativeRoot 'venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Prepared workspace runtime missing. Use the Compose setup in README.' }
& $taskPython (Join-Path $PSScriptRoot 'run_native_demo.py') --native-root $taskNativeRoot --mode $Mode
