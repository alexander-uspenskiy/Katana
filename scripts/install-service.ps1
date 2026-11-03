# Installs (or regenerates) the Task Scheduler task that runs Katana while you are logged in.
#
# The task runs `orchestrator.py dispatch` every KATANA_DISPATCH_INTERVAL seconds (60), which starts a
# background worker for each project that is due. Run this again if you move the Katana folder.
# Windows has no way to watch a folder for the task, so a new requirement file waits for the next check.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& python "$root\factory\service.py" install
exit $LASTEXITCODE
