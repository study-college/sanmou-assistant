$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = & python -c "import sys,PySide6,rapidocr_onnxruntime;from pathlib import Path;assert (3,11)<=sys.version_info[:2]<(3,13); print(Path(sys.executable).with_name('pythonw.exe'))" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $pythonExe) {
        $pythonExe = & py -3.11 -c "import sys,PySide6,rapidocr_onnxruntime;from pathlib import Path;print(Path(sys.executable).with_name('pythonw.exe'))" 2>$null
    }
    if ($LASTEXITCODE -ne 0 -or -not $pythonExe) { throw 'Run scripts/setup-source.ps1 first, or use the packaged executable.' }
    $pythonExe = $pythonExe.Trim()
}
$dataRoot = $projectRoot
$consolePython = Join-Path (Split-Path -Parent $pythonExe) 'python.exe'
& $consolePython -c "import sys,PIL; assert (3,11)<=sys.version_info[:2]<(3,13), 'Need standard Python 3.11-3.12'; assert int(PIL.__version__.split('.')[0])>=13, 'Run setup-source.ps1 to install patched Pillow'"
if ($LASTEXITCODE -ne 0) { throw 'Run scripts/setup-source.ps1 with standard CPython 3.11-3.12 before starting this version.' }
if (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $projectRoot) 'release-manifest.json')) { $dataRoot = Split-Path -Parent $projectRoot }
# This is the interactive main window and must be visible to the user.
Start-Process -FilePath $pythonExe -ArgumentList @(('"' + (Join-Path $projectRoot 'run_assistant.py') + '"'), '--data-dir', ('"' + (Join-Path $dataRoot 'data') + '"')) -WorkingDirectory $projectRoot -WindowStyle Normal
