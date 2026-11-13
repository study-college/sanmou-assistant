"""Export a new, sanitized product repository without traversing local user data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = {".py", ".md", ".ps1", ".json", ".toml", ".txt", ".cmd", ".css", ".svg", ".yml"}
SCRIPTS = ("build.ps1", "setup-source.ps1", "start-source.ps1", "package_release.py", "verify_release.ps1", "render_guide.py", "prepare_github.py", "audit_publication.py")


def export(output: Path) -> dict:
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite an existing directory: {output}")
    mapping_file = ROOT / "artifacts/release-anonymization.local.json"
    mapping = json.loads(mapping_file.read_text("utf-8")) if mapping_file.exists() else {}
    output.mkdir(parents=True)
    files = []

    def copy(path: Path, relative: Path | str | None = None):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Expected a regular product file: {path}")
        target = output / (relative or path.relative_to(ROOT))
        target.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        if path.suffix in TEXT:
            text = content.decode("utf-8-sig")
            for private, anonymous in mapping.items():
                text = text.replace(private, anonymous)
            content = text.replace("\r\n", "\n").encode("utf-8-sig" if path.suffix == ".ps1" else "utf-8")
        target.write_bytes(content)
        files.append({"path": target.relative_to(output).as_posix(), "sha256": hashlib.sha256(content).hexdigest()})

    for path in sorted((ROOT / "sanmou_assistant").rglob("*")):
        if path.is_file() and (path.suffix == ".py" or "assets" in path.relative_to(ROOT / "sanmou_assistant").parts[:-1] and path.suffix in (".png", ".json", ".ico")):
            copy(path)
    for name in ("README.md", "pyproject.toml", "run_assistant.py", "requirements-runtime.txt", "requirements-build.txt", "requirements-lock.txt", "启动助手.cmd", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md"):
        copy(ROOT / name)
    for name in SCRIPTS:
        copy(ROOT / "scripts" / name)
    for path in sorted(list((ROOT / "tests").glob("*.py")) + list((ROOT / "research-tests").glob("*.py"))):
        text = path.read_text("utf-8")
        # 依赖本机 artifacts/data 的测试无法在公开仓库运行，且可能含本机截图里的账号与坐标；
        # 一律不导出，公开仓库只保留可独立运行的测试。
        if path.parent.name == "research-tests" or "artifacts" in text or "data/sessions" in text:
            continue
        copy(path, Path("tests") / path.name)
    from sanmou_assistant import __version__
    for name in dict.fromkeys(("09-user-guide.md", "AGENT-GUIDE.md", "ROADMAP.md", "RELEASE-0.8.1.md", "RELEASE-0.8.2.md", "RELEASE-0.8.3.md", "RELEASE-0.9.0.md", "RELEASE-0.9.2.md", "RELEASE-0.9.3.md", "RELEASE-0.9.4.md", "RELEASE-0.9.5.md", "RELEASE-0.9.6.md", "RELEASE-0.9.9.md", f"RELEASE-{__version__}.md", "guide.css")):
        copy(ROOT / "docs" / name)
    for path in sorted((ROOT / "docs/guide-assets").glob("*.png")):
        copy(path)
    copy(ROOT / "docs/AGENT-GUIDE.md", "AGENTS.md")
    for path in sorted((ROOT / ".github").rglob("*")):
        if path.is_file() and path.suffix in (".yml", ".md"):
            copy(path)
    for path in sorted((ROOT / "docs/assets").glob("*.svg")):
        copy(path)
    ignore = """# Runtime data, account settings, screenshots, secrets and generated files
data/
profiles/
artifacts/
build/
dist/
Netease/
.venv/
venv/
__pycache__/
.pytest_cache/
*.py[cod]
*.egg-info/
*.sqlite*
*.db
*.log
*.bak*
.env
.env.*
!.env.example
*.pem
*.key
*.pfx
*.p12
feedback*.zip
crash.log
.idea/
.vscode/
Thumbs.db
"""
    (output / ".gitignore").write_text(ignore, "utf-8")
    (output / ".gitattributes").write_text("* text=auto\n*.py text eol=lf\n*.md text eol=lf\n*.json text eol=lf\n*.toml text eol=lf\n*.ps1 text eol=crlf\n*.cmd text eol=crlf\n*.png binary\n", "utf-8")
    return {"output": str(output), "files": files, "private_tests": len(list((output / "research-tests").glob("*.py")))}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.output.resolve()), ensure_ascii=False))
