"""Unified multi-account runtime controller shared by GUI and headless server modes."""
from __future__ import annotations

import copy
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal

from . import __version__
from .accounts import AccountStore
from .models import Point, Profile, TEAM_ROLE_LABELS
from .target_ops import (
    format_targets_brief,
    merge_live_target_fields,
    replace_targets,
    set_backup_point,
    summarize_targets,
    switch_primary_target,
)
from .worker import Worker

UNSET = object()
SUPPORTED_START_COMMANDS = ("run", "scan", "return", "farming", "dailies", "calibrate", "rates", "review", "preview")


class ControlError(Exception):
    """Structured error returned to GUI callers, HTTP API clients, and CLI."""

    def __init__(self, message: str, *, code: str = "invalid_request", http_status: int = 400):
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.http_status = http_status


class _MainThreadInvoker(QObject):
    """Safely marshals calls from HTTP server threads onto the Qt main thread."""

    _dispatch = Signal(object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._dispatch.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    def _execute(self, payload: tuple[Callable[[], Any], list, threading.Event]) -> None:
        fn, holder, done = payload
        try:
            holder.append((True, fn()))
        except BaseException as exc:  # noqa: BLE001
            holder.append((False, exc))
        finally:
            done.set()

    def call(self, fn: Callable[[], Any], timeout: float = 30.0) -> Any:
        app = QCoreApplication.instance()
        if app is None or QThread.currentThread() == self.thread():
            return fn()
        holder: list[tuple[bool, Any]] = []
        done = threading.Event()
        self._dispatch.emit((fn, holder, done))
        if not done.wait(timeout):
            raise ControlError("主线程响应超时，请稍后重试", code="timeout", http_status=504)
        ok, result = holder[0]
        if ok:
            return result
        raise result


class AccountRuntimeSnapshot:
    """In-memory live snapshot of one account's execution state."""

    LOG_LIMIT = 200

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.lock = threading.RLock()
        self.running = False
        self.paused = False
        self.command = ""
        self.dry_run = True
        self.started_at: Optional[float] = None
        self.session_dir = ""
        self.status = "待启动"
        self.connection: Optional[dict] = None
        self.grain: Optional[int] = None
        self.resource_visible = False
        self.resource_observed_at: Optional[float] = None
        self.merit: Optional[dict] = None
        self.orders = 0
        self.active_slots: Optional[list[int]] = None
        self.observations: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.team_activity: dict[str, dict] = {}
        self.pave_progress: dict[str, dict] = {}
        self.recent_logs: deque[dict] = deque(maxlen=self.LOG_LIMIT)
        self.last_error = ""
        self.latest_evidence = ""
        self.last_updated_at = time.time()

    def mark_started(self, *, command: str, dry_run: bool, session_dir: Path | str) -> None:
        with self.lock:
            self.running = True
            self.paused = False
            self.command = command
            self.dry_run = dry_run
            self.started_at = time.time()
            self.session_dir = str(session_dir)
            self.status = "正在启动本次任务"
            self.last_error = ""
            if command in ("run", "scan"):
                self.observations.clear()
                self.decisions.clear()
                self.team_activity.clear()
                self.pave_progress.clear()
                self.orders = 0
                self.merit = None
            self.recent_logs.clear()
            self.last_updated_at = time.time()

    def mark_finished(self) -> None:
        with self.lock:
            self.running = False
            self.paused = False
            self.last_updated_at = time.time()

    def mark_paused(self, paused: bool) -> None:
        with self.lock:
            self.paused = bool(paused)
            self.last_updated_at = time.time()

    def append_log(self, level: str, message: str, when: str = "") -> None:
        with self.lock:
            self.recent_logs.append({
                "time": when or datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "message": str(message),
            })
            self.last_updated_at = time.time()

    def record_event(self, kind: str, value: Any) -> None:
        if kind == "preview":
            return
        with self.lock:
            self.last_updated_at = time.time()
            if kind == "status":
                text = str(value)
                if self.status != text:
                    self.status = text
                    self.recent_logs.append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "level": "INFO",
                        "message": text,
                    })
            elif kind == "log" and isinstance(value, dict):
                self.recent_logs.append({
                    "time": str(value.get("time") or datetime.now().strftime("%H:%M:%S")),
                    "level": str(value.get("level") or "INFO"),
                    "message": str(value.get("message") or ""),
                })
            elif kind == "connection" and isinstance(value, dict):
                self.connection = dict(value)
            elif kind == "grain":
                self.grain = value
            elif kind == "resource_status" and isinstance(value, dict):
                self.resource_visible = bool(value.get("visible"))
                self.resource_observed_at = value.get("observed_at")
            elif kind == "merit" and isinstance(value, dict):
                self.merit = dict(value)
            elif kind == "team_presence" and isinstance(value, dict):
                slots = value.get("slots")
                self.active_slots = sorted(int(s) for s in slots) if slots is not None else None
            elif kind == "observation" and isinstance(value, dict):
                key = str(value.get("key") or "")
                if key:
                    self.observations[key] = dict(value)
                    self.team_activity.pop(key, None)
                    self.decisions.pop(key, None)
            elif kind == "decision" and isinstance(value, dict):
                team = str(value.get("team") or "")
                if team:
                    self.decisions[team] = dict(value)
            elif kind == "dispatch" and isinstance(value, dict):
                self.orders += 1
                team = str(value.get("team") or "")
                if team:
                    self.team_activity[team] = {
                        "state": value.get("state", "confirmed"),
                        "action": value.get("action", "出征"),
                        "target": value.get("target"),
                    }
            elif kind == "team_action" and isinstance(value, dict):
                team = str(value.get("team") or "")
                if team:
                    self.team_activity[team] = dict(value)
            elif kind == "pave" and isinstance(value, dict):
                team = str(value.get("team") or "")
                if team:
                    self.pave_progress[team] = dict(value)
            elif kind == "evidence":
                self.latest_evidence = str(value)
            elif kind == "error":
                self.last_error = str(value)

    def to_status_dict(self, profile: Profile, *, account_name: str, selected: bool) -> dict:
        with self.lock:
            teams = []
            for team in profile.teams:
                if team.group != "main":
                    continue
                present = self.active_slots is None or team.slot in self.active_slots
                obs = self.observations.get(team.key)
                dec = self.decisions.get(team.key)
                act = self.team_activity.get(team.key)
                teams.append({
                    "key": team.key,
                    "slot": team.slot,
                    "display": team.display,
                    "role": team.role,
                    "role_label": TEAM_ROLE_LABELS.get(team.role, team.role),
                    "present": present,
                    "min_mode": team.min_mode,
                    "minimum": team.minimum,
                    "observation": copy.deepcopy(obs) if obs else None,
                    "decision": copy.deepcopy(dec) if dec else None,
                    "activity": copy.deepcopy(act) if act else None,
                })
            return {
                "account_id": self.account_id,
                "name": account_name,
                "account": profile.account,
                "selected": selected,
                "instance": str(profile.instance),
                "package": profile.package,
                "main_city": (
                    {"name": profile.main_city.name, "x": profile.main_city.x, "y": profile.main_city.y}
                    if profile.main_city else None
                ),
                "running": self.running,
                "paused": self.paused,
                "command": self.command,
                "dry_run": self.dry_run,
                "started_at": self.started_at,
                "session_dir": self.session_dir,
                "status": self.status,
                "connection": copy.deepcopy(self.connection),
                "grain": self.grain,
                "resource_visible": self.resource_visible,
                "resource_observed_at": self.resource_observed_at,
                "merit": copy.deepcopy(self.merit),
                "orders": self.orders,
                "active_slots": list(self.active_slots) if self.active_slots is not None else None,
                "teams": teams,
                "pave_progress": copy.deepcopy(self.pave_progress),
                "targets": summarize_targets(profile),
                "targets_brief": format_targets_brief(profile),
                "last_error": self.last_error,
                "latest_evidence": self.latest_evidence,
                "last_updated_at": self.last_updated_at,
            }

    def logs(self, limit: int = 50) -> list[dict]:
        with self.lock:
            n = max(1, min(int(limit), self.LOG_LIMIT))
            return list(self.recent_logs)[-n:]


def _instance_conflict(running_items: list[tuple[str, Profile]], target_account_id: str,
                       profile: Profile, command: str) -> Optional[str]:
    if command in ("discover", "runtime", "pave-reset"):
        return None
    cli = os.path.normcase(os.path.abspath(profile.cli_path)) if profile.cli_path else ""
    for other_id, other_profile in running_items:
        if other_id == target_account_id:
            continue
        other_cli = os.path.normcase(os.path.abspath(other_profile.cli_path)) if other_profile.cli_path else ""
        if str(other_profile.instance) == str(profile.instance) and other_cli == cli:
            owner = other_profile.account or other_profile.name or other_id
            return f"实例 {profile.instance} 正由账号“{owner}”使用，请先停止该账号任务"
    return None


class HeadlessHubBackend:
    """Headless multi-account Worker manager for `--command serve`."""

    def __init__(self, data_dir: Path, *, worker_factory=None):
        self.data_dir = Path(data_dir).resolve()
        self.store = AccountStore(self.data_dir)
        self.worker_factory = worker_factory or Worker
        self.workers: dict[str, Worker] = {}
        self.profiles: dict[str, Profile] = {}
        self.paused: dict[str, bool] = {}
        self.hub: Optional["RuntimeHub"] = None

    def attach_hub(self, hub: "RuntimeHub") -> None:
        self.hub = hub

    def list_account_contexts(self):
        return self.store.accounts

    def selected_account_id(self) -> str:
        return self.store.selected

    def get_account_context(self, account_id: str):
        try:
            return self.store.get(account_id)
        except ValueError as exc:
            raise ControlError(str(exc), code="account_not_found", http_status=404) from exc

    def get_profile(self, account_id: str) -> Profile:
        account = self.get_account_context(account_id)
        worker = self.get_worker(account_id)
        if worker is not None:
            return copy.deepcopy(worker.profile)
        if account_id in self.profiles:
            return copy.deepcopy(self.profiles[account_id])
        profile = account.load_profile()
        self.profiles[account_id] = copy.deepcopy(profile)
        return profile

    def get_worker(self, account_id: str) -> Optional[Worker]:
        worker = self.workers.get(account_id)
        if worker is not None and worker.isRunning():
            return worker
        return None

    def is_running(self, account_id: str) -> bool:
        return self.get_worker(account_id) is not None

    def is_paused(self, account_id: str) -> bool:
        return bool(self.paused.get(account_id, False)) and self.is_running(account_id)

    def save_and_sync_profile(self, account_id: str, profile: Profile, *, source: str, live_applied: bool) -> None:
        self.store.save_profile(account_id, profile)
        self.profiles[account_id] = copy.deepcopy(profile)

    def start_worker(self, account_id: str, *, command: str, execute: bool,
                     max_orders: int = 0, duration: int = 0) -> Path:
        account = self.get_account_context(account_id)
        if self.is_running(account_id):
            raise ControlError("当前账号的任务仍在运行，请先停止后再启动", code="already_running", http_status=409)
        profile = account.load_profile()
        dry_run = (not execute) or command not in ("run", "return", "dailies", "farming")
        errors = profile.validate(active=(command == "run" and not dry_run))
        if errors:
            raise ControlError("；".join(errors), code="validation_error", http_status=400)
        running_items = [(aid, w.profile) for aid, w in self.workers.items() if w.isRunning()]
        conflict = _instance_conflict(running_items, account_id, profile, command)
        if conflict:
            raise ControlError(conflict, code="instance_conflict", http_status=409)
        folder = account.sessions_path / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        worker = self.worker_factory(profile, folder, command, dry_run, max_orders, duration)
        self.profiles[account_id] = copy.deepcopy(profile)
        self.workers[account_id] = worker
        self.paused[account_id] = False
        if self.hub is not None:
            self.hub.bind_worker(account_id, worker, command=command, dry_run=dry_run, session_dir=folder)
        worker.start()
        return folder

    def stop_worker(self, account_id: str) -> bool:
        worker = self.get_worker(account_id)
        if worker is None:
            return False
        worker.stop()
        return True

    def set_paused(self, account_id: str, paused: bool) -> None:
        worker = self.get_worker(account_id)
        if worker is None:
            raise ControlError("当前账号未在运行，无法暂停或恢复", code="not_running", http_status=409)
        worker.pause(paused)
        self.paused[account_id] = bool(paused)
        if self.hub is not None:
            snap = self.hub.snapshot(account_id)
            snap.mark_paused(paused)
            snap.append_log("INFO", "任务已暂停（保持连接与初始化状态）" if paused else "任务已恢复，立即进入下一轮")

    def stop_all(self, wait_ms: int = 15000) -> None:
        running = [w for w in self.workers.values() if w.isRunning()]
        for worker in running:
            worker.stop()
        deadline = time.monotonic() + wait_ms / 1000.0
        for worker in running:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if worker.isRunning() and remaining > 0:
                worker.wait(remaining)


class GuiHubBackend:
    """Adapter that exposes an active AccountManagerWindow through RuntimeHub."""

    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.hub: Optional["RuntimeHub"] = None

    def attach_hub(self, hub: "RuntimeHub") -> None:
        self.hub = hub

    def list_account_contexts(self):
        return self.store.accounts

    def selected_account_id(self) -> str:
        return self.store.selected

    def _window_for(self, account_id: str):
        for window in self.manager.windows:
            if window.account_id == account_id:
                return window
        raise ControlError(f"账号不存在：{account_id}", code="account_not_found", http_status=404)

    def get_account_context(self, account_id: str):
        try:
            return self.store.get(account_id)
        except ValueError as exc:
            raise ControlError(str(exc), code="account_not_found", http_status=404) from exc

    def get_profile(self, account_id: str) -> Profile:
        window = self._window_for(account_id)
        if window.worker and window.worker.isRunning():
            return copy.deepcopy(window.profile)
        return copy.deepcopy(window.profile)

    def get_worker(self, account_id: str) -> Optional[Worker]:
        window = self._window_for(account_id)
        if window.worker and window.worker.isRunning():
            return window.worker
        return None

    def is_running(self, account_id: str) -> bool:
        return self.get_worker(account_id) is not None

    def is_paused(self, account_id: str) -> bool:
        window = self._window_for(account_id)
        return bool(window.paused) and self.is_running(account_id)

    def save_and_sync_profile(self, account_id: str, profile: Profile, *, source: str, live_applied: bool) -> None:
        window = self._window_for(account_id)
        self.store.save_profile(account_id, profile)
        window.sync_targets_from_profile(profile, source=source, live_applied=live_applied)
        self.manager.update_account(window)

    def start_worker(self, account_id: str, *, command: str, execute: bool,
                     max_orders: int = 0, duration: int = 0) -> Path:
        window = self._window_for(account_id)
        if window.worker and window.worker.isRunning():
            raise ControlError("当前账号的任务仍在运行，请先停止后再启动", code="already_running", http_status=409)
        dry_run = (not execute) or command not in ("run", "return", "dailies", "farming")
        return window.start_from_controller(
            command,
            dry_run=dry_run,
            max_orders=max_orders,
            duration=duration,
        )

    def stop_worker(self, account_id: str) -> bool:
        window = self._window_for(account_id)
        if not (window.worker and window.worker.isRunning()):
            return False
        window.stop()
        return True

    def set_paused(self, account_id: str, paused: bool) -> None:
        window = self._window_for(account_id)
        if not (window.worker and window.worker.isRunning()):
            raise ControlError("当前账号未在运行，无法暂停或恢复", code="not_running", http_status=409)
        window.set_paused(paused)
        if self.hub is not None:
            snap = self.hub.snapshot(account_id)
            snap.mark_paused(paused)


class RuntimeHub:
    """Thread-safe runtime control facade shared by GUI, HTTP server, and headless mode."""

    def __init__(self, backend, *, mode: str = "headless", parent: Optional[QObject] = None):
        self.backend = backend
        self.mode = mode
        self.invoker = _MainThreadInvoker(parent)
        self._snapshots_lock = threading.RLock()
        self._snapshots: dict[str, AccountRuntimeSnapshot] = {}
        if hasattr(backend, "attach_hub"):
            backend.attach_hub(self)

    def snapshot(self, account_id: str) -> AccountRuntimeSnapshot:
        with self._snapshots_lock:
            snap = self._snapshots.get(account_id)
            if snap is None:
                snap = AccountRuntimeSnapshot(account_id)
                self._snapshots[account_id] = snap
            return snap

    def bind_worker(self, account_id: str, worker: Worker, *, command: str,
                    dry_run: bool, session_dir: Path | str) -> None:
        snap = self.snapshot(account_id)
        snap.mark_started(command=command, dry_run=dry_run, session_dir=session_dir)

        def on_event(kind: str, value: Any) -> None:
            snap.record_event(kind, value)
            if kind == "calibrated" and isinstance(value, dict) and self.mode == "headless":
                try:
                    profile = self.backend.get_profile(account_id)
                    profile.account = value["account"]
                    profile.main_city = Point(**value["main_city"])
                    if not profile.validate():
                        self.backend.save_and_sync_profile(account_id, profile, source="calibrated", live_applied=False)
                except Exception:  # noqa: BLE001
                    pass

        worker.event.connect(on_event)
        worker.finished.connect(snap.mark_finished)

    def _resolve_id(self, account_id: str = "") -> str:
        text = (account_id or "").strip()
        if not text or text == "selected":
            return self.backend.selected_account_id()
        self.backend.get_account_context(text)
        return text

    def list_accounts(self) -> list[dict]:
        def _do():
            selected = self.backend.selected_account_id()
            items = []
            for account in self.backend.list_account_contexts():
                profile = self.backend.get_profile(account.id)
                snap = self.snapshot(account.id)
                items.append({
                    "account_id": account.id,
                    "name": account.name,
                    "account": profile.account,
                    "selected": account.id == selected,
                    "instance": str(profile.instance),
                    "running": self.backend.is_running(account.id),
                    "paused": self.backend.is_paused(account.id),
                    "status": snap.status,
                    "targets_brief": format_targets_brief(profile),
                })
            return items

        return self.invoker.call(_do)

    def get_status(self, account_id: str = "") -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            account = self.backend.get_account_context(aid)
            profile = self.backend.get_profile(aid)
            snap = self.snapshot(aid)
            return snap.to_status_dict(
                profile,
                account_name=account.name,
                selected=(aid == self.backend.selected_account_id()),
            )

        return self.invoker.call(_do)

    def get_targets(self, account_id: str = "") -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            profile = self.backend.get_profile(aid)
            return {
                "account_id": aid,
                "running": self.backend.is_running(aid),
                "targets": summarize_targets(profile),
                "targets_brief": format_targets_brief(profile),
            }

        return self.invoker.call(_do)

    def _commit_target_change(self, aid: str, candidate: Profile, *, source: str) -> dict:
        worker = self.backend.get_worker(aid)
        running = worker is not None
        active = bool(running and worker.command == "run" and not worker.dry_run)
        base = self.backend.get_profile(aid)
        try:
            merged, changed = merge_live_target_fields(base, candidate, active=active)
        except ValueError as exc:
            raise ControlError(str(exc), code="validation_error", http_status=400) from exc

        live_applied = False
        if running:
            if worker.command != "run":
                raise ControlError(
                    f"当前账号正在执行短期任务（{worker.command}），请等待结束或停止后再修改目标",
                    code="busy_short_command",
                    http_status=409,
                )
            # 先写盘，再推给运行中的 Worker；写盘失败则绝不造成内存与磁盘分叉。
            try:
                self.backend.save_and_sync_profile(aid, merged, source=source, live_applied=True)
            except OSError as exc:
                raise ControlError(f"配置保存失败：{exc}", code="io_error", http_status=500) from exc
            worker.queue_target_update(merged, changed=changed or {"merit", "line"}, source=source)
            live_applied = True
        else:
            try:
                self.backend.save_and_sync_profile(aid, merged, source=source, live_applied=False)
            except OSError as exc:
                raise ControlError(f"配置保存失败：{exc}", code="io_error", http_status=500) from exc
        return {
            "account_id": aid,
            "running": running,
            "live_applied": live_applied,
            "changed": sorted(changed),
            "source": source,
            "targets": summarize_targets(merged),
            "targets_brief": format_targets_brief(merged),
        }

    def switch_target(
        self,
        account_id: str = "",
        *,
        mode: str,
        address: str = "",
        name: str = "",
        x: Optional[int] = None,
        y: Optional[int] = None,
        actions=None,
        keep_others: bool = False,
        source: str = "agent",
    ) -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            worker = self.backend.get_worker(aid)
            active = bool(worker is not None and worker.command == "run" and not worker.dry_run)
            base = self.backend.get_profile(aid)
            try:
                updated, chosen = switch_primary_target(
                    base,
                    mode=mode,
                    address=address,
                    name=name,
                    x=x,
                    y=y,
                    actions=actions,
                    keep_others=keep_others,
                    active=active,
                )
            except ValueError as exc:
                raise ControlError(str(exc), code="validation_error", http_status=400) from exc
            result = self._commit_target_change(aid, updated, source=source)
            result["mode"] = mode
            result["selected_target"] = {
                "name": chosen.name,
                "x": chosen.x,
                "y": chosen.y,
                "actions": list(chosen.actions),
                "enabled": chosen.enabled,
            }
            return result

        return self.invoker.call(_do)

    def update_targets(
        self,
        account_id: str = "",
        *,
        targets: Any = UNSET,
        line_targets: Any = UNSET,
        backup_enabled: Any = UNSET,
        merit_backup: Any = UNSET,
        line_backups: Any = UNSET,
        source: str = "agent",
    ) -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            worker = self.backend.get_worker(aid)
            active = bool(worker is not None and worker.command == "run" and not worker.dry_run)
            current = self.backend.get_profile(aid)
            try:
                if targets is not UNSET:
                    current = replace_targets(current, mode="merit", points=targets, active=False)
                if line_targets is not UNSET:
                    current = replace_targets(current, mode="line", points=line_targets, active=False)
                if backup_enabled is not UNSET:
                    if type(backup_enabled) is not bool:
                        raise ValueError("backup_enabled 必须是布尔值")
                    current.backup_enabled = backup_enabled
                if merit_backup is not UNSET:
                    if merit_backup is None or merit_backup == "":
                        current = set_backup_point(current, mode="merit", clear=True, active=False)
                    elif isinstance(merit_backup, str):
                        current = set_backup_point(current, mode="merit", address=merit_backup, active=False)
                    elif isinstance(merit_backup, dict):
                        current = set_backup_point(
                            current,
                            mode="merit",
                            address=str(merit_backup.get("address") or ""),
                            name=str(merit_backup.get("name") or ""),
                            x=merit_backup.get("x"),
                            y=merit_backup.get("y"),
                            active=False,
                        )
                    else:
                        raise ValueError("merit_backup 必须是地址字符串、对象或 null")
                if line_backups is not UNSET:
                    if not isinstance(line_backups, dict):
                        raise ValueError("line_backups 必须是按队伍索引的对象")
                    for team_key, entry in line_backups.items():
                        if entry is None or entry == "":
                            current = set_backup_point(current, mode="line", team_key=team_key, clear=True, active=False)
                        elif isinstance(entry, str):
                            current = set_backup_point(current, mode="line", team_key=team_key, address=entry, active=False)
                        elif isinstance(entry, dict):
                            current = set_backup_point(
                                current,
                                mode="line",
                                team_key=team_key,
                                address=str(entry.get("address") or ""),
                                name=str(entry.get("name") or ""),
                                x=entry.get("x"),
                                y=entry.get("y"),
                                active=False,
                            )
                        else:
                            raise ValueError(f"拉线征兵点 {team_key!r} 格式不正确")
                errors = current.validate(active=active)
                if errors:
                    raise ValueError("；".join(errors))
            except ValueError as exc:
                raise ControlError(str(exc), code="validation_error", http_status=400) from exc
            return self._commit_target_change(aid, current, source=source)

        return self.invoker.call(_do)

    def set_backup(
        self,
        account_id: str = "",
        *,
        mode: str,
        team_key: str = "",
        address: str = "",
        name: str = "",
        x: Optional[int] = None,
        y: Optional[int] = None,
        clear: bool = False,
        enabled: Optional[bool] = None,
        source: str = "agent",
    ) -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            worker = self.backend.get_worker(aid)
            active = bool(worker is not None and worker.command == "run" and not worker.dry_run)
            base = self.backend.get_profile(aid)
            try:
                updated = set_backup_point(
                    base,
                    mode=mode,
                    team_key=team_key,
                    address=address,
                    name=name,
                    x=x,
                    y=y,
                    clear=clear,
                    enabled=enabled,
                    active=active,
                )
            except ValueError as exc:
                raise ControlError(str(exc), code="validation_error", http_status=400) from exc
            result = self._commit_target_change(aid, updated, source=source)
            result["mode"] = mode
            return result

        return self.invoker.call(_do)

    def apply_live_profile(self, account_id: str, candidate: Profile, *, source: str = "ui") -> dict:
        """GUI 目标页运行中直接提交表单合并热更新的入口。"""
        return self.invoker.call(lambda: self._commit_target_change(self._resolve_id(account_id), candidate, source=source))

    def start_account(
        self,
        account_id: str = "",
        *,
        command: str = "run",
        execute: bool = True,
        max_orders: int = 0,
        duration: int = 0,
    ) -> dict:
        if command not in SUPPORTED_START_COMMANDS:
            raise ControlError(
                f"不支持的启动命令 {command!r}；可选：{', '.join(SUPPORTED_START_COMMANDS)}",
                code="invalid_command",
                http_status=400,
            )
        if type(max_orders) is not int or max_orders < 0 or type(duration) is not int or duration < 0:
            raise ControlError("max_orders 与 duration 必须是非负整数", code="validation_error", http_status=400)

        def _do():
            aid = self._resolve_id(account_id)
            folder = self.backend.start_worker(
                aid,
                command=command,
                execute=bool(execute),
                max_orders=max_orders,
                duration=duration,
            )
            return {
                "account_id": aid,
                "started": True,
                "command": command,
                "execute": bool(execute),
                "session_dir": str(folder),
            }

        return self.invoker.call(_do)

    def stop_account(self, account_id: str = "") -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            stopped = self.backend.stop_worker(aid)
            return {"account_id": aid, "stop_requested": stopped, "was_running": stopped}

        return self.invoker.call(_do)

    def pause_account(self, account_id: str = "", *, paused: bool) -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            self.backend.set_paused(aid, bool(paused))
            return {"account_id": aid, "paused": bool(paused)}

        return self.invoker.call(_do)

    def wake_account(self, account_id: str = "") -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            worker = self.backend.get_worker(aid)
            if worker is None:
                raise ControlError("当前账号未在运行，无法唤醒", code="not_running", http_status=409)
            worker.wake()
            return {"account_id": aid, "woken": True}

        return self.invoker.call(_do)

    def refresh_account(self, account_id: str = "") -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            worker = self.backend.get_worker(aid)
            if worker is None:
                raise ControlError("当前账号未在运行，无法刷新", code="not_running", http_status=409)
            worker.request_refresh()
            return {"account_id": aid, "refresh_requested": True}

        return self.invoker.call(_do)

    def get_logs(self, account_id: str = "", *, limit: int = 50) -> dict:
        def _do():
            aid = self._resolve_id(account_id)
            snap = self.snapshot(aid)
            return {"account_id": aid, "running": self.backend.is_running(aid), "logs": snap.logs(limit)}

        return self.invoker.call(_do)

    def health(self) -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "mode": self.mode,
            "pid": os.getpid(),
        }
