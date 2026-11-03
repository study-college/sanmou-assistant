param([string]$Python = "python")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$distributionExe = Join-Path $projectRoot 'dist\SanmouAssistant\SanmouAssistant.exe'
$runningAssistant = Get-Process -Name SanmouAssistant -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $distributionExe }
if ($runningAssistant) { throw '当前发行程序正在运行，请先停止任务并关闭该程序后构建；未覆盖现有程序。' }
$taskOriginalPath = $env:PATH
Push-Location -LiteralPath $projectRoot
try {
    $pythonCommand = (Get-Command $Python -ErrorAction Stop).Source
    $qtLibraryRoot = & $Python -c "import pathlib,PySide6; print(pathlib.Path(PySide6.__file__).parent)"
    # 只对本次构建进程收窄 PATH。其它工具的 ICU/Qt DLL 不能混入发行包。
    $env:PATH = @((Split-Path -Parent $pythonCommand), $qtLibraryRoot, (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $Python -m PyInstaller --noconfirm --clean --name SanmouAssistant --windowed --onedir --add-data "sanmou_assistant/assets;sanmou_assistant/assets" --collect-data rapidocr_onnxruntime --collect-binaries onnxruntime --copy-metadata PySide6 --copy-metadata rapidocr-onnxruntime --copy-metadata onnxruntime --copy-metadata opencv-python --copy-metadata numpy --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module matplotlib --exclude-module pandas --exclude-module scipy --exclude-module torch --exclude-module tensorflow --exclude-module IPython --exclude-module pytest --exclude-module sympy run_assistant.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\09-user-guide.md') -Destination (Join-Path $projectRoot 'dist\SanmouAssistant\使用说明.md')
    Copy-Item -LiteralPath (Join-Path $projectRoot '启动助手.cmd') -Destination (Join-Path $projectRoot 'dist\SanmouAssistant\启动助手.cmd')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\AGENT-GUIDE.md') -Destination (Join-Path $projectRoot 'dist\SanmouAssistant\AGENT-GUIDE.md')
} finally {
    $env:PATH = $taskOriginalPath
    Pop-Location
}

