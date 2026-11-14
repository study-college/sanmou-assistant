"""Export a new, sanitized product repository without traversing local user data."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = {".py", ".md", ".ps1", ".json", ".toml", ".txt", ".cmd", ".css", ".svg", ".yml"}
SCRIPTS = ("build.ps1", "setup-source.ps1", "start-source.ps1", "package_release.py", "verify_release.ps1", "render_guide.py", "prepare_github.py", "audit_publication.py")
ROOT_FILES = ("README.md", "pyproject.toml", "run_assistant.py", "requirements-runtime.txt", "requirements-build.txt", "requirements-lock.txt", "启动助手.cmd", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md")
MAPPING_FILE = "artifacts/release-anonymization.local.json"


def load_mapping(*, allow_no_anonymization: bool) -> dict:
    """本机脱敏映射缺失时默认失败，避免无声降级；只有已脱敏公开源码/CI 才允许显式跳过。"""
    mapping_file = ROOT / MAPPING_FILE
    if not mapping_file.exists():
        if allow_no_anonymization:
            return {}
        raise RuntimeError(
            f"Missing anonymization mapping: {mapping_file}. Create it locally, or pass "
            "--allow-no-anonymization only when exporting already-anonymized public source (e.g. CI).")
    mapping = json.loads(mapping_file.read_text("utf-8"))
    if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
        raise RuntimeError(f"Anonymization mapping must be a JSON object of string keys and values: {mapping_file}")
    return mapping


def audit_export(output: Path, mapping: dict):
    """导出后用 mapping keys 作为 private markers 审计导出目录，任何命中都必须失败。"""
    if not mapping:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from audit_publication import Audit, check_repository
    audit = Audit(list(mapping))
    check_repository(audit, output)
    if audit.issues:
        raise RuntimeError("Exported source still contains private markers or paths: "
                           + json.dumps(audit.issues, ensure_ascii=False))


def export(output: Path, *, allow_no_anonymization: bool = False) -> dict:
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite an existing directory: {output}")
    mapping = load_mapping(allow_no_anonymization=allow_no_anonymization)
    output.mkdir(parents=True)
    files = []
    private_tests = []

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
    for name in ROOT_FILES:
        copy(ROOT / name)
    for name in SCRIPTS:
        copy(ROOT / "scripts" / name)
    for path in sorted(list((ROOT / "tests").glob("*.py")) + list((ROOT / "research-tests").glob("*.py"))):
        text = path.read_text("utf-8")
        # 依赖本机 artifacts/data 的测试无法在公开仓库运行，且可能含本机截图里的账号与坐标；
        # 一律不导出，公开仓库只保留可独立运行的测试。
        if path.parent.name == "research-tests" or "artifacts" in text or "data/sessions" in text:
            private_tests.append(path.name)
            continue
        copy(path, Path("tests") / path.name)
    from sanmou_assistant import __version__
    docs = ("09-user-guide.md", "AGENT-GUIDE.md", "ROADMAP.md", "RELEASE-0.8.1.md", "RELEASE-0.8.2.md", "RELEASE-0.8.3.md", "RELEASE-0.9.0.md", "RELEASE-0.9.2.md", "RELEASE-0.9.3.md", "RELEASE-0.9.4.md", "RELEASE-0.9.5.md", "RELEASE-0.9.6.md", "RELEASE-0.9.9.md", f"RELEASE-{__version__}.md", "guide.css")
    for name in dict.fromkeys(docs):
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
    audit_export(output, mapping)
    return {"output": str(output), "files": files, "private_tests": len(private_tests)}


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-no-anonymization", action="store_true",
                        help="Allow exporting without artifacts/release-anonymization.local.json; only for already-anonymized public source or CI")
    args = parser.parse_args()
    print(json.dumps(export(args.output.resolve(), allow_no_anonymization=args.allow_no_anonymization), ensure_ascii=False))
