"""Assemble only allowlisted public product files; never recurse through user data."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import sys
import uuid
import zipfile
from datetime import datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sanmou_assistant import __version__


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def text_file(path, value, *, powershell=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig" if powershell else "utf-8"))


def assemble(app, output, anonymize=None):
    if output.exists() or Path(str(output) + ".zip").exists():
        raise RuntimeError(f"Refusing to overwrite an existing release: {output}")
    if not (app / "SanmouAssistant.exe").is_file():
        raise RuntimeError("Build app/SanmouAssistant.exe first")
    for path in app.rglob("*"):
        if path.is_symlink() or path.relative_to(app).parts[0] in ("data", "sessions") or path.name == "active-profile.json" or path.suffix.lower() in (".sqlite", ".sqlite3"):
            raise RuntimeError(f"Unexpected user data or link in app directory: {path}")
    stage = output.parent / (".release-stage-" + uuid.uuid4().hex[:8])
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / "app")
    source = stage / "source"
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md", ".gitignore", ".gitattributes"):
        copy_file(ROOT / name, source / name)
        if name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"):
            copy_file(ROOT / name, stage / name)
    for path in (ROOT / "sanmou_assistant").rglob("*"):
        if path.is_file() and (path.suffix == ".py" or "assets" in path.relative_to(ROOT / "sanmou_assistant").parts[:-1] and path.suffix in (".png", ".json")):
            copy_file(path, source / path.relative_to(ROOT))
    for name in ("pyproject.toml", "run_assistant.py", "requirements-runtime.txt", "requirements-build.txt", "requirements-lock.txt", "启动助手.cmd"):
        copy_file(ROOT / name, source / name)
    for name in ("build.ps1", "setup-source.ps1", "start-source.ps1", "package_release.py", "verify_release.ps1", "render_guide.py", "audit_publication.py", "prepare_github.py"):
        path = ROOT / "scripts" / name
        if path.suffix == ".ps1":
            text_file(source / "scripts" / name, path.read_text(encoding="utf-8-sig"), powershell=True)
        else:
            copy_file(path, source / "scripts" / name)
    fixture_tests = []
    for path in sorted(list((ROOT / "tests").glob("*.py")) + list((ROOT / "research-tests").glob("*.py"))):
        private = path.parent.name == "research-tests" or "artifacts" in path.read_text(encoding="utf-8") or "data/sessions" in path.read_text(encoding="utf-8")
        public_test = path.read_text(encoding="utf-8")
        for original, anonymous in (anonymize or {}).items():
            public_test = public_test.replace(original, anonymous)
        text_file(source / ("research-tests" if private else "tests") / path.name, public_test)
        if private:
            fixture_tests.append(path.name)
    for name in dict.fromkeys(("09-user-guide.md", "AGENT-GUIDE.md", "ROADMAP.md", "RELEASE-0.8.1.md", f"RELEASE-{__version__}.md")):
        copy_file(ROOT / "docs" / name, source / "docs" / name)
        public_name = "使用说明.md" if name == "09-user-guide.md" else name
        copy_file(ROOT / "docs" / name, stage / "docs" / public_name)
        if name == "09-user-guide.md":
            copy_file(ROOT / "docs" / name, stage / "docs" / name)
        if name in ("09-user-guide.md", "AGENT-GUIDE.md"):
            copy_file(ROOT / "docs" / name, stage / "app" / ("使用说明.md" if name == "09-user-guide.md" else name))
    copy_file(ROOT / "docs" / "AGENT-GUIDE.md", source / "AGENTS.md")
    copy_file(ROOT / "README.md", source / "README.md")
    for path in (ROOT / ".github").rglob("*"):
        if path.is_file() and path.suffix in (".yml", ".md"):
            copy_file(path, source / path.relative_to(ROOT))
    for path in (ROOT / "docs/assets").glob("*.svg"):
        copy_file(path, source / path.relative_to(ROOT))
    text_file(stage / "校验完整性.ps1", (ROOT / "scripts" / "verify_release.ps1").read_text(encoding="utf-8-sig"), powershell=True)
    commands = {
        "启动助手.cmd": 'start "" "%~dp0app\\SanmouAssistant.exe" --data-dir "%~dp0data"\n',
        "启动源码.cmd": 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0source\\scripts\\start-source.ps1"\nif errorlevel 1 pause\n',
        "安装源码环境.cmd": 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0source\\scripts\\setup-source.ps1"\npause\n',
        "导出诊断.cmd": 'start "" /wait "%~dp0app\\SanmouAssistant.exe" --data-dir "%~dp0data" --command diagnostics --output "%~dp0feedback.zip"\nif errorlevel 1 (echo 导出失败，请检查data/crash.log。) else (echo 已生成根目录feedback.zip，请核对后再反馈。)\npause\n',
    }
    for name, body in commands.items():
        text_file(stage / name, '@echo off\nchcp 65001 >nul\nsetlocal\ncd /d "%~dp0"\n' + body)
    copy_file(ROOT / "docs" / "guide.css", source / "docs" / "guide.css")
    assets = ROOT / "docs" / "guide-assets"
    if assets.is_dir():
        for asset in assets.glob("*.png"):
            copy_file(asset, source / "docs" / "guide-assets" / asset.name)
            copy_file(asset, stage / "docs" / "guide-assets" / asset.name)
    sys.path.insert(0, str(ROOT / "scripts"))
    from render_guide import write_guides
    write_guides(stage, ROOT, __version__)
    text_file(stage / "请先阅读.txt", "三谋助手 " + __version__ + " Windows x64完整分发包\n\n先完整解压，然后双击 启动助手.cmd。无需Python或AI。\n首次使用请打开 先读我.html，按步骤设置自己的MuMu、实例ID、渠道、账号和主城。\nAI请先读 docs/AGENT-GUIDE.md。源码环境首次安装需Python3.11–3.12 x64及网络。\n没有携带作者账号配置、数据库或截图。首次运行创建自己的data。\n停止和退出不撤军、不关闭模拟器。\n")
    license_root = stage / "licenses"
    runtime_packages = ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6", "rapidocr-onnxruntime", "onnxruntime", "numpy", "opencv-python", "Pillow", "Shapely", "pyclipper", "PyYAML", "six", "tqdm", "coloredlogs", "humanfriendly", "flatbuffers", "protobuf", "packaging", "sympy", "mpmath", "pyreadline3", "pyinstaller", "pyinstaller-hooks-contrib")
    for name in runtime_packages:
        dist = metadata.distribution(name)
        for item in dist.files or []:
            lower = str(item).lower()
            if any(token in lower for token in ("license", "copying", "copyright", "notice")):
                path = Path(dist.locate_file(item))
                if path.is_file() and path.suffix.lower() not in (".py", ".pyc", ".pyd", ".dll") and path.stat().st_size < 2 * 1024 * 1024:
                    safe_name = str(item).replace("..", "_").replace("\\", "/").replace("/", "__")
                    copy_file(path, license_root / name / safe_name)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        copy_file(python_license, license_root / "Python-LICENSE.txt")
    text_file(license_root / "README.txt", "Third-party components retain their own licenses.\nQt/PySide6 libraries are dynamically linked and remain replaceable in app/_internal.\nQt/PySide6 source and license information: https://www.qt.io/qt-for-python and https://download.qt.io/official_releases/QtForPython/\nOCR runtime/model information: https://github.com/RapidAI/RapidOCR\nThis package does not contain MuMu, the game, or user credentials.\n")
    files = []
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(stage).as_posix(), "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {"version": __version__, "created": datetime.now().isoformat(),
                "target": "Windows 10/11 x64", "emulator_baseline": "MuMu 5.30.1.3586",
                "private_fixture_tests": fixture_tests, "files": files}
    text_file(stage / "release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    stage.rename(output)
    archive_path = Path(str(output) + ".zip")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(output.name) / path.relative_to(output)).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP integrity failure: {bad}")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    text_file(archive_path.with_suffix(".zip.sha256"), digest + "  " + archive_path.name + "\n")
    print(json.dumps({"archive": str(archive_path), "sha256": digest, "bytes": archive_path.stat().st_size,
                      "files": len(files) + 1}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", type=Path, default=ROOT / "dist" / "SanmouAssistant")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"SanmouAssistant-{__version__}-Windows-x64-full")
    parser.add_argument("--anonymize", type=Path, help="Optional local JSON mapping for private test fixture names; this file is not distributed")
    args = parser.parse_args()
    mapping_path = args.anonymize or ROOT / "artifacts" / "release-anonymization.local.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.is_file() else {}
    if not isinstance(mapping, dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in mapping.items()):
        raise ValueError("Anonymization mapping must contain string keys and values")
    assemble(args.app_dir.resolve(), args.output.resolve(), mapping)
