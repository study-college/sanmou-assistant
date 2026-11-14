"""Local, bounded support reports. No device connection or network upload."""
from __future__ import annotations

import html
import json
import platform
import sqlite3
import sys
import traceback
import zipfile
from collections import Counter
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import __version__


def environment():
    packages = {}
    for name in ("PySide6", "opencv-python", "numpy", "onnxruntime", "rapidocr-onnxruntime"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "bundled" if getattr(sys, "frozen", False) else "missing"
    return {"version": __version__, "python": sys.version, "os": platform.platform(),
            "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False)),
            "dependencies": packages}


def tail(path: Path, limit=2 * 1024 * 1024):
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        if size > limit:
            stream.readline()
        return stream.read().decode("utf-8", errors="replace")


def read_events(path: Path):
    records = []
    if path.is_file():
        for line in tail(path).splitlines()[-2000:]:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    return records


def all_events(path: Path):
    """流式读取整份事件文件；长会话的计数不能只算尾部 2000 行。"""
    if not path.is_file():
        return
    with path.open("rb") as stream:
        for raw in stream:
            try:
                yield json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue


def event_counts(path: Path):
    counts = Counter()
    for record in all_events(path):
        counts[record.get("event")] += 1
    return counts


def _is_attention(record):
    return record.get("event") in ("error", "diagnostic") or (
        record.get("event") == "log" and isinstance(record.get("value"), dict)
        and record["value"].get("level") in ("WARN", "ERROR"))


def _attention_metadata(record):
    """安全诊断只保留告警的时间、事件类型和日志级别，绝不带原始 value/message。"""
    value = record.get("value")
    return {"at": record.get("at"), "event": record.get("event"),
            "level": value.get("level") if isinstance(value, dict) else None}


def write_session_report(folder: Path, error=""):
    records = read_events(folder / "events.jsonl")
    attention = [r for r in records if r.get("event") in ("error", "diagnostic") or
                 (r.get("event") == "log" and r.get("value", {}).get("level") in ("WARN", "ERROR"))]
    summary = {"environment": environment(), "error": error,
               "session": folder.name, "event_counts": dict(event_counts(folder / "events.jsonl")),
               "attention": attention[-40:], "last_events": records[-20:]}
    (folder / "diagnosis.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    body = html.escape(json.dumps(summary, ensure_ascii=False, indent=2))
    (folder / "诊断摘要.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>三谋助手诊断摘要</title>'
        '<style>body{font:15px sans-serif;max-width:1100px;margin:30px auto}pre{white-space:pre-wrap}</style>'
        '<h1>三谋助手诊断摘要</h1><p>本地生成。反馈时在软件日志页导出诊断包，可一起带上近期截图。</p>'
        f'<pre>{body}</pre>', encoding="utf-8")
    return summary


def export_diagnostics(data: Path, destination: Path, include_private: bool = False):
    """include_private=False 导出安全诊断包：只有环境、会话名、事件计数、告警元数据和文件清单，
    不收原始事件值、配置、日志、截图、SQLite 导出等可能含昵称、坐标或本机路径的内容；
    include_private=True 才导出完整诊断包（配置、订单、原始日志、最多12张截图）。"""
    data = data.resolve()
    sources = [("sessions", data / "sessions", data / "active-profile.json", "journal.json", "images")]
    accounts_root = data / "accounts"
    if accounts_root.is_dir():
        for root in sorted(accounts_root.glob("account-*")):
            if root.is_dir():
                account_id = root.name
                sources.append((f"accounts/{account_id}/sessions", root / "sessions", root / "profile.json",
                                f"accounts/{account_id}/journal.json", f"images/{account_id}"))
    entries = {}
    records = []
    session_names = {}
    event_totals = Counter()
    attention_meta = []
    file_manifest = {}
    image_sources = []
    for prefix, sessions, profile, journal_name, image_prefix in sources:
        folders = sorted((p for p in sessions.iterdir() if p.is_dir() and p.name[:8].isdigit()),
                         key=lambda p: p.name, reverse=True)[:3] if sessions.is_dir() else []
        session_names[prefix] = [p.name for p in folders]
        for folder in folders:
            event_totals.update(event_counts(folder / "events.jsonl"))
            for record in read_events(folder / "events.jsonl"):
                if _is_attention(record):
                    attention_meta.append(_attention_metadata(record))
                if include_private:
                    records.append(record)
            for name in ("events.jsonl", "result.json", "session.json", "diagnosis.json", "诊断摘要.html"):
                path = folder / name
                if path.is_file():
                    file_manifest[f"{prefix}/{folder.name}/{name}"] = path.stat().st_size
                    if include_private:
                        entries[f"{prefix}/{folder.name}/{name}"] = tail(path).encode("utf-8")
        if profile.is_file():
            file_manifest[profile.relative_to(data).as_posix()] = profile.stat().st_size
            if include_private:
                entries[profile.relative_to(data).as_posix()] = tail(profile).encode("utf-8")
        database = sessions / "runtime.sqlite"
        if database.is_file():
            file_manifest[database.relative_to(data).as_posix()] = database.stat().st_size
        if include_private:
            db_report = {}
            if database.is_file():
                try:
                    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                        db.row_factory = sqlite3.Row
                        for name in ("intents", "pave", "phases", "daily", "flags", "task_context"):
                            db_report[name] = [dict(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid DESC LIMIT 100")]
                except sqlite3.Error as exc:
                    db_report["read_error"] = str(exc)
            entries[journal_name] = json.dumps(db_report, ensure_ascii=False, indent=2).encode("utf-8")
        image_sources.append((sessions, image_prefix))
    for name in ("accounts.json", "crash.log"):
        path = data / name
        if path.is_file():
            file_manifest[name] = path.stat().st_size
            if include_private:
                entries[name] = tail(path).encode("utf-8")
    images = []
    image_bytes = 0
    if include_private:
        records.sort(key=lambda record: record.get("at", ""))
        # Only event-referenced images inside an account's own sessions; never follow arbitrary paths.
        for record in reversed(records):
            if record.get("event") != "evidence" or not isinstance(record.get("value"), str):
                continue
            path = Path(record["value"]).resolve()
            source = next((row for row in image_sources if path.is_relative_to(row[0].resolve())), None)
            if path.suffix.lower() != ".png" or source is None or not path.is_file():
                continue
            key = source[1] + "/" + path.name
            if key in entries or path.stat().st_size + image_bytes > 16 * 1024 * 1024:
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue  # Worker可能刚好滚动淘汰了这张截图。
            entries[key] = content
            image_bytes += len(content)
            images.append(key)
            if len(images) >= 12:
                break
        report = {"created": datetime.now().isoformat(), "environment": environment(), "mode": "full",
                  "sessions": session_names.get("sessions", []), "account_sessions": session_names, "images": images,
                  "attention": [r for r in records if _is_attention(r)][-60:]}
        description = ('<h1>三谋助手问题反馈（完整诊断包）</h1><p>包含版本、近期日志、配置、订单状态和最多12张截图；不包含登录凭据或完整数据库。'
                       '配置和截图可能含游戏昵称、坐标与本机路径，请核对后再发送给维护者。</p>')
    else:
        attention_meta.sort(key=lambda item: item.get("at") or "")
        report = {"created": datetime.now().isoformat(), "environment": environment(), "mode": "safe",
                  "sessions": session_names.get("sessions", []), "account_sessions": session_names,
                  "event_counts": dict(event_totals), "attention": attention_meta[-60:], "files": file_manifest}
        description = ('<h1>三谋助手问题反馈（安全诊断包）</h1><p>仅包含版本与运行环境、会话名称、事件计数、近期告警的时间/类型/级别和数据文件清单；'
                       '不含账号配置、原始日志、订单、截图、游戏坐标或本机路径，可直接发送给维护者。</p>')
    entries["report.json"] = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    images_html = "".join(f'<p>{html.escape(name)}</p><img width="960" src="{html.escape(name)}">' for name in images)
    entries["请先打开诊断报告.html"] = (
        '<!doctype html><meta charset="utf-8"><title>三谋助手问题反馈</title>'
        '<style>body{font:15px sans-serif;margin:24px}pre{white-space:pre-wrap}img{max-width:100%}</style>'
        f'{description}<pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2))}</pre>{images_html}').encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return report


def install_exception_log(data: Path):
    def handle(kind, value, tb):
        path = data / "crash.log"
        try:
            if path.exists() and path.stat().st_size > 1024 * 1024:
                path.replace(data / "crash.previous.log")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n{datetime.now().isoformat()} v{__version__}\n")
                traceback.print_exception(kind, value, tb, file=stream)
        except OSError:
            pass
        finally:
            sys.__excepthook__(kind, value, tb)
        if sys.stderr is None and sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"程序发生异常：{value}\n\n诊断记录：{path}\n请保留记录并反馈给维护者。", "三谋助手", 0x10)
    sys.excepthook = handle
