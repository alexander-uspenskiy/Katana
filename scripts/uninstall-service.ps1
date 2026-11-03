# Stops and removes the Katana Task Scheduler task. Runs and input files are kept.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& python "$root\factory\service.py" uninstall
exit $LASTEXITCODE
