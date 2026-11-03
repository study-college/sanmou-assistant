"""Rotate disposable screenshots independently of durable order records."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid


MIB = 1024 ** 2
LIMITS = {"routine": 128 * MIB, "actions": 128 * MIB, "faults": 32 * MIB}
ROUTINE_INTERVAL = 600
MAX_AGE = 7 * 86400
FAULT_WORDS = ("unknown", "unconfirmed", "unrecognized", "unreadable", "uncertain",
               "blocked", "not-recognized", "timeout", "failed", "failure", "refused",
               "unavailable", "reconnect-limit", "network-disconnected")


class EvidenceLimitError(RuntimeError):
    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = path


def unresolved(connection):
    """All scopes: an old unresolved order must survive a later session/account."""
    return bool(connection.execute(
        "SELECT 1 FROM intents WHERE status IN ('prepared','uncertain','pending') LIMIT 1").fetchone()
        or connection.execute(
            "SELECT 1 FROM daily WHERE status IN ('prepared','uncertain','pending') LIMIT 1").fetchone())


class EvidenceStore:
    def __init__(self, folder: Path, protected=lambda: False, *, limits=None, clock=time.time):
        folder = Path(folder).resolve()
        # Only this new namespace is managed. Existing dated sessions/artifacts stay intact.
        base = folder.parent if folder.parent.name == "sessions" else folder
        self.root = base / "_evidence"
        self.session = folder.name
        self.protected = protected
        self.limits = dict(LIMITS if limits is None else limits)
        self.clock = clock
        self.last_routine = {}
        self.pruned_files = self.pruned_bytes = 0

    @staticmethod
    def category(name):
        if any(word in name.lower() for word in FAULT_WORDS):
            return "faults"
        return "routine" if re.fullmatch(r"(?:main|guard)_\d+-(?:located|home)", name) else "actions"

    def due(self, name):
        return self.category(name) != "routine" or self.clock() - self.last_routine.get(name, float("-inf")) >= ROUTINE_INTERVAL

    @staticmethod
    def _not_link(path):
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise EvidenceLimitError("截图保留目录出现联接或符号链接，停止清理和新操作：" + str(path))

    def files(self, category):
        base = self.root / category
        for parent in (self.root, base):
            if os.path.lexists(parent):
                self._not_link(parent)
        if not base.exists():
            return []
        files = []
        for folder, dirs, names in os.walk(base, followlinks=False):
            for name in dirs + names:
                self._not_link(Path(folder) / name)
            for name in names:
                path = Path(folder) / name
                info = path.stat()
                files.append((info.st_mtime, info.st_size, path))
        return sorted(files)

    def prune(self, category, incoming=0):
        files = self.files(category)
        total = sum(row[1] for row in files)
        cutoff = self.clock() - MAX_AGE
        for modified, size, path in files:
            if modified >= cutoff and total + incoming <= self.limits[category]:
                continue
            if path.suffix != ".png":
                continue
            self._not_link(path)
            if not path.resolve().is_relative_to(self.root.resolve()):
                raise EvidenceLimitError("截图清理路径越界，已停止")
            path.unlink()
            total -= size
            self.pruned_files += 1
            self.pruned_bytes += size
        return total

    def check_capacity(self):
        # Screenshot housekeeping must never gate device startup.
        for category in self.limits:
            try:
                self.prune(category)
            except (OSError, EvidenceLimitError):
                pass

    def write(self, name, payload):
        name = re.sub(r"[^\w.-]", "_", name)
        if not self.due(name):
            return None
        category = self.category(name)
        if len(payload) > self.limits[category]:
            return None
        self.prune(category, len(payload))
        directory = self.root / category / self.session
        directory.mkdir(parents=True, exist_ok=True)
        self._not_link(directory)
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S_%f")
        path = directory / f"{stamp}-{uuid.uuid4().hex[:8]}-{name}.png"
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if category == "routine":
            self.last_routine[name] = self.clock()
        return path


def main():
    parser = argparse.ArgumentParser(description="查看新版截图占用；--prune 对所有类别滚动删除旧截图，不修改订单记录")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--prune", action="store_true", help="请先停止助手；只处理新版截图目录")
    args = parser.parse_args()
    data = args.data_dir.resolve(strict=True)
    roots = [data]
    accounts = data / "accounts"
    if accounts.is_dir():
        roots.extend(sorted(path for path in accounts.glob("account-*") if path.is_dir()))
    for root in roots:
        database = root / "sessions/runtime.sqlite"
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) if database.exists() else None
        try:
            store = EvidenceStore(root / "sessions/maintenance", lambda connection=connection: unresolved(connection) if connection else True)
            if args.prune:
                for category in LIMITS:
                    store.prune(category)
            print(json.dumps({"root": str(store.root), "bytes": {k: sum(x[1] for x in store.files(k)) for k in LIMITS},
                              "limits": LIMITS, "pruned_files": store.pruned_files, "pruned_bytes": store.pruned_bytes}, ensure_ascii=False))
        finally:
            if connection:
                connection.close()


if __name__ == "__main__":
    main()
