param([string]$Python = "")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not $Python) {
    foreach ($version in @('3.12','3.11','3.10')) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            try {
                $candidate = & py "-$version" -c 'import sys;print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $candidate = $candidate.Trim()
                    & $candidate -c "import sys,pathlib; sys.exit(1 if 'anaconda' in sys.version.lower() or (pathlib.Path(sys.base_prefix)/'conda-meta').exists() else 0)" 2>$null
                    if ($LASTEXITCODE -eq 0) { $Python = $candidate; break }
                }
            } catch { continue }
        }
    }
}
if (-not $Python) { $Python = 'python' }
& $Python -c "import sys; assert (3,10)<=sys.version_info[:2]<(3,13), 'Need Python 3.10-3.12 x64'; assert sys.maxsize>2**32, 'Need x64 Python'"
if ($LASTEXITCODE -ne 0) { throw 'Please install Python 3.10-3.12 x64, or use the packaged executable.' }
& $Python -c "import sys,pathlib; sys.exit(1 if 'anaconda' in sys.version.lower() or (pathlib.Path(sys.base_prefix)/'conda-meta').exists() else 0)"
if ($LASTEXITCODE -ne 0) { throw 'Please use standard CPython 3.10-3.12 x64 from python.org. Conda Qt/DLL loading is not supported; the packaged app needs no Python.' }
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $selectedBase = & $Python -c 'import sys;print(sys.base_prefix)'
    $existingBase = & $venvPython -c 'import sys;print(sys.base_prefix)'
    if ($LASTEXITCODE -ne 0 -or $selectedBase.Trim() -ne $existingBase.Trim()) {
        throw 'Existing .venv uses another Python. Move .venv aside and rerun; do not mix interpreter versions in one environment.'
    }
}
& $Python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
& $venvPython -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; check the network and rerun this script.' }
& $venvPython -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw 'Source installation failed' }
& $venvPython -c "from PySide6.QtWidgets import QApplication;import cv2,onnxruntime,rapidocr_onnxruntime;print('Qt/OCR imports ready')"
if ($LASTEXITCODE -ne 0) { throw 'Qt/OCR runtime import failed. Preserve this output and use the packaged app or a clean standard CPython environment.' }
Write-Host 'Ready. Run the source launcher or .venv\Scripts\python.exe -m pytest -q.'
