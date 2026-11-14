"""Check publication paths and text, optionally including Git history or a ZIP."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_DIRS = {"data", "sessions", "profiles", "artifacts", "Netease", ".venv"}
IGNORED = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", "build", "dist"}
RULES = {
    "github-token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/=\r\n]{64,}-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "api-key": re.compile(rb"(?:sk-proj-[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})"),
    "personal-windows-home": re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b|Default\b|<|example\b|test\b)[A-Za-z0-9_.-]+", re.I),
    # 本机项目根目录约定；lookbehind 排除 URL 路径段和普通相对目录，MuMu 等公开安装示例不误伤。
    "local-projects-dir": re.compile(rb"(?<![A-Za-z0-9_/.-])(?:[A-Za-z]:[\\/]+|~[\\/]+|/)Projects[\\/]+[A-Za-z0-9_.-]+", re.I),
}


def private_path(name: str) -> bool:
    p = PurePosixPath(name.replace("\\", "/"))
    # OpenCV ships its own non-user package data under this exact vendor path.
    vendor_data = any(p.parts[i:i + 4] == ("app", "_internal", "cv2", "data") for i in range(len(p.parts)))
    return (".." in p.parts or p.is_absolute()
            or any(part in PRIVATE_DIRS and not (part == "data" and vendor_data) for part in p.parts)
            or p.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".pem", ".key", ".pfx", ".p12", ".log"}
            or p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example")


class Audit:
    def __init__(self, markers: list[str]):
        self.markers = [m.encode("utf-8") for m in markers if m]
        self.issues = []
        self.checked = 0

    def check(self, name: str, content: bytes, *, paths: bool = True):
        self.checked += 1
        if paths and private_path(name):
            self.issues.append({"path": name, "rule": "private-path"})
        if any(m in content for m in self.markers):
            self.issues.append({"path": name, "rule": "private-identity"})
        parts = PurePosixPath(name.replace("\\", "/")).parts
        vendor = any(parts[i:i + 2] == ("app", "_internal") for i in range(len(parts)))
        for rule, pattern in RULES.items():
            # Upstream wheels contain their own public build-machine paths.
            # Local private markers above still apply to every inspected file.
            if rule == "personal-windows-home" and vendor:
                continue
            if pattern.search(content):
                self.issues.append({"path": name, "rule": rule})


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


def check_repository(audit: Audit, repo: Path):
    if (repo / ".git").exists():
        names = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").strip("\0").split("\0")
        paths = [repo / name for name in names if name]
    else:
        paths = [p for p in repo.rglob("*") if p.is_file() and not any(x in IGNORED for x in p.relative_to(repo).parts)]
    for path in paths:
        if path.is_symlink():
            audit.issues.append({"path": path.relative_to(repo).as_posix(), "rule": "link"})
        elif path.is_file():
            audit.check(path.relative_to(repo).as_posix(), path.read_bytes())


def check_history(audit: Audit, repo: Path):
    objects = git(repo, "rev-list", "--objects", "--all").decode("utf-8").splitlines()
    process = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for entry in objects:
            oid, _, name = entry.partition(" ")
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii").split()
            size = int(header[2])
            content = process.stdout.read(size)
            process.stdout.read(1)
            if header[1] in ("blob", "commit"):
                audit.check("history:" + oid[:12] + "/" + name, content, paths=header[1] == "blob")
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()


def check_archive(audit: Audit, archive: Path):
    with zipfile.ZipFile(archive) as source:
        if source.testzip() is not None:
            audit.issues.append({"path": archive.name, "rule": "zip-crc"})
        for entry in source.infolist():
            if entry.is_dir():
                continue
            # Large runtime libraries are built on the runner; inspect their paths
            # and inspect text/source/license files in full without allocating DLLs.
            content = source.read(entry) if entry.file_size < 4 * 1024 * 1024 else b""
            audit.check(entry.filename, content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--private-markers", type=Path, help="Local-only JSON list; never upload this file")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    markers = json.loads(args.private_markers.read_text("utf-8")) if args.private_markers else []
    if not isinstance(markers, list) or any(not isinstance(x, str) for x in markers):
        parser.error("Private markers must be a JSON list of strings")
    audit = Audit(markers)
    if args.repository:
        check_repository(audit, args.repository.resolve())
        if args.history:
            check_history(audit, args.repository.resolve())
    elif args.history:
        parser.error("--history requires --repository")
    if args.archive:
        check_archive(audit, args.archive)
    if not args.repository and not args.archive:
        parser.error("Provide --repository or --archive")
    report = {"ok": not audit.issues, "items_checked": audit.checked, "issues": audit.issues}
    value = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(value, "utf-8")
    print(value)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
