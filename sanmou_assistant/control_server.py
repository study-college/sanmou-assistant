"""Local loopback JSON control server, client (with offline fallback), and `ctl` CLI."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .accounts import AccountStore
from .controller import ControlError, RuntimeHub, UNSET
from .models import atomic_json, data_directory
from .target_ops import (
    format_targets_brief,
    set_backup_point,
    summarize_targets,
    switch_primary_target,
)

CONTROL_FILE_NAME = "control-server.json"
MAX_REQUEST_BYTES = 256 * 1024


def control_file_path(data_dir: Path) -> Path:
    return Path(data_dir).resolve() / CONTROL_FILE_NAME


def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ControlServer:
    """Loopback-only HTTP JSON server bound to 127.0.0.1 with per-session bearer token."""

    def __init__(self, hub: RuntimeHub, data_dir: Path, *, host: str = "127.0.0.1",
                 port: int = 0, mode: str = "gui"):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("本地控制服务仅允许绑定 127.0.0.1")
        self.hub = hub
        self.data_dir = Path(data_dir).resolve()
        self.host = "127.0.0.1"
        self.requested_port = int(port)
        self.mode = mode
        self.token = secrets.token_hex(32)
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port: int = 0
        self.info_path = control_file_path(self.data_dir)
        self._closed = False

    def start(self) -> dict:
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

            def _send_json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return False
                provided = header[7:].strip()
                return bool(provided) and hmac.compare_digest(provided, server_ref.token)

            def _read_json_body(self) -> dict:
                raw_len = self.headers.get("Content-Length", "0").strip()
                if not raw_len:
                    return {}
                try:
                    length = int(raw_len)
                except ValueError as exc:
                    raise ControlError("无效的 Content-Length", code="invalid_request", http_status=400) from exc
                if length < 0 or length > MAX_REQUEST_BYTES:
                    raise ControlError("请求体过大或长度无效", code="payload_too_large", http_status=413)
                if length == 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ControlError("请求体不是合法的 UTF-8 JSON", code="invalid_json", http_status=400) from exc
                if not isinstance(parsed, dict):
                    raise ControlError("请求体必须是 JSON 对象", code="invalid_json", http_status=400)
                return parsed

            def _handle(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = urllib.parse.parse_qs(parsed.query)
                if method == "GET" and path == "/v1/health":
                    self._send_json(200, {"ok": True, "data": server_ref.hub.health()})
                    return
                if not self._authorized():
                    self._send_json(401, {"ok": False, "code": "unauthorized", "error": "缺少或无效的本机访问令牌"})
                    return
                try:
                    data = self._route(method, path, query)
                except ControlError as exc:
                    self._send_json(exc.http_status, {"ok": False, "code": exc.code, "error": exc.message})
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "code": "validation_error", "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"ok": False, "code": "internal_error", "error": str(exc)})
                else:
                    self._send_json(200, {"ok": True, "data": data})

            def _route(self, method: str, path: str, query: dict[str, list[str]]) -> Any:
                hub = server_ref.hub
                if method == "GET" and path == "/v1/accounts":
                    return {"accounts": hub.list_accounts()}

                parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]
                if len(parts) < 4 or parts[0] != "v1" or parts[1] != "accounts":
                    raise ControlError(f"未知接口：{method} {path}", code="not_found", http_status=404)
                account_id = parts[2]
                sub = "/".join(parts[3:])

                if method == "GET" and sub == "status":
                    return hub.get_status(account_id)
                if method == "GET" and sub == "targets":
                    return hub.get_targets(account_id)
                if method == "GET" and sub == "logs":
                    raw_limit = (query.get("limit") or ["50"])[0]
                    try:
                        limit = int(raw_limit)
                    except ValueError as exc:
                        raise ControlError("limit 必须是整数", code="validation_error", http_status=400) from exc
                    return hub.get_logs(account_id, limit=limit)
                if method == "POST" and sub == "targets/switch":
                    body = self._read_json_body()
                    return hub.switch_target(
                        account_id,
                        mode=str(body.get("mode") or ""),
                        address=str(body.get("address") or ""),
                        name=str(body.get("name") or ""),
                        x=body.get("x"),
                        y=body.get("y"),
                        actions=body.get("actions"),
                        keep_others=bool(body.get("keep_others", False)),
                        source=str(body.get("source") or "agent"),
                    )
                if method == "PUT" and sub == "targets":
                    body = self._read_json_body()
                    return hub.update_targets(
                        account_id,
                        targets=body["targets"] if "targets" in body else UNSET,
                        line_targets=body["line_targets"] if "line_targets" in body else UNSET,
                        backup_enabled=body["backup_enabled"] if "backup_enabled" in body else UNSET,
                        merit_backup=body["merit_backup"] if "merit_backup" in body else UNSET,
                        line_backups=body["line_backups"] if "line_backups" in body else UNSET,
                        source=str(body.get("source") or "agent"),
                    )
                if method == "POST" and sub == "backups":
                    body = self._read_json_body()
                    return hub.set_backup(
                        account_id,
                        mode=str(body.get("mode") or ""),
                        team_key=str(body.get("team_key") or body.get("team") or ""),
                        address=str(body.get("address") or ""),
                        name=str(body.get("name") or ""),
                        x=body.get("x"),
                        y=body.get("y"),
                        clear=bool(body.get("clear", False)),
                        enabled=body.get("enabled") if "enabled" in body else None,
                        source=str(body.get("source") or "agent"),
                    )
                if method == "POST" and sub == "start":
                    body = self._read_json_body()
                    return hub.start_account(
                        account_id,
                        command=str(body.get("command") or "run"),
                        execute=bool(body.get("execute", True)),
                        max_orders=int(body.get("max_orders", 0)),
                        duration=int(body.get("duration", 0)),
                    )
                if method == "POST" and sub == "stop":
                    return hub.stop_account(account_id)
                if method == "POST" and sub == "pause":
                    return hub.pause_account(account_id, paused=True)
                if method == "POST" and sub == "resume":
                    return hub.pause_account(account_id, paused=False)
                if method == "POST" and sub == "wake":
                    return hub.wake_account(account_id)
                if method == "POST" and sub == "refresh":
                    return hub.refresh_account(account_id)
                raise ControlError(f"未知接口：{method} {path}", code="not_found", http_status=404)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PUT(self) -> None:
                self._handle("PUT")

        self.server = ThreadingHTTPServer((self.host, self.requested_port), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        info = {
            "schema": 1,
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "mode": self.mode,
            "version": __version__,
            "started_at": datetime.now().isoformat(),
        }
        atomic_json(self.info_path, info)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                       name="SanmouControlServer", daemon=True)
        self.thread.start()
        return info

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None
        # 仅清理属于当前进程与令牌的连接文件，不误删后启动实例的文件。
        try:
            if self.info_path.is_file():
                raw = json.loads(self.info_path.read_text(encoding="utf-8"))
                if raw.get("pid") == os.getpid() and raw.get("token") == self.token:
                    self.info_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


class ControlClient:
    """Client for local ControlServer with safe offline fallback for config operations."""

    def __init__(self, data_dir: Path, *, timeout: float = 15.0):
        self.data_dir = Path(data_dir).resolve()
        self.timeout = timeout
        self.info_path = control_file_path(self.data_dir)
        self._opener = _no_proxy_opener()

    def discover(self) -> Optional[dict]:
        if not self.info_path.is_file():
            return None
        try:
            info = json.loads(self.info_path.read_text(encoding="utf-8"))
            host = str(info.get("host") or "127.0.0.1")
            port = int(info.get("port") or 0)
            token = str(info.get("token") or "")
        except (OSError, ValueError, TypeError):
            return None
        if host not in ("127.0.0.1", "localhost") or port <= 0 or not token:
            return None
        base_url = f"http://{host}:{port}"
        try:
            req = urllib.request.Request(f"{base_url}/v1/health", method="GET")
            with self._opener.open(req, timeout=min(2.0, self.timeout)) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if resp.status == 200 and body.get("ok"):
                return {"base_url": base_url, "token": token, "health": body.get("data", {}), "info": info}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        return None

    def request(self, method: str, path: str, payload: Optional[dict] = None, *, server: Optional[dict] = None) -> dict:
        conn = server or self.discover()
        if conn is None:
            raise ControlError(
                "未发现正在运行的三谋助手控制服务；请先打开桌面助手，或使用 `--command serve` 启动后台托管服务",
                code="server_not_running",
                http_status=503,
            )
        url = conn["base_url"] + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {conn['token']}"}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                raise ControlError(
                    parsed.get("error") or raw or f"HTTP {exc.code}",
                    code=parsed.get("code") or "http_error",
                    http_status=exc.code,
                ) from exc
            except ValueError:
                raise ControlError(raw or f"HTTP {exc.code}", code="http_error", http_status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ControlError(f"连接本地控制服务失败：{exc}", code="connection_error", http_status=503) from exc
        if not parsed.get("ok"):
            raise ControlError(parsed.get("error") or "请求失败", code=parsed.get("code") or "error", http_status=400)
        return parsed["data"]

    def _account_segment(self, account_id: str) -> str:
        return urllib.parse.quote((account_id or "selected").strip() or "selected", safe="")

    # ---- High-level methods with offline fallback where safe ----

    def list_accounts(self) -> dict:
        conn = self.discover()
        if conn is not None:
            data = self.request("GET", "/v1/accounts", server=conn)
            data["source"] = "live"
            return data
        store = AccountStore(self.data_dir)
        items = []
        for account in store.accounts:
            profile = account.load_profile()
            items.append({
                "account_id": account.id,
                "name": account.name,
                "account": profile.account,
                "selected": account.id == store.selected,
                "instance": str(profile.instance),
                "running": False,
                "paused": False,
                "status": "离线（控制服务未运行）",
                "targets_brief": format_targets_brief(profile),
            })
        return {"accounts": items, "source": "offline"}

    def get_targets(self, account_id: str = "") -> dict:
        conn = self.discover()
        if conn is not None:
            data = self.request("GET", f"/v1/accounts/{self._account_segment(account_id)}/targets", server=conn)
            data["source"] = "live"
            return data
        store = AccountStore(self.data_dir)
        try:
            account = store.get(account_id or store.selected)
        except ValueError as exc:
            raise ControlError(str(exc), code="account_not_found", http_status=404) from exc
        profile = account.load_profile()
        return {
            "account_id": account.id,
            "running": False,
            "live_applied": False,
            "source": "offline",
            "targets": summarize_targets(profile),
            "targets_brief": format_targets_brief(profile),
        }

    def switch_target(self, *, account_id: str = "", mode: str, address: str = "", name: str = "",
                      x: Optional[int] = None, y: Optional[int] = None, actions=None,
                      keep_others: bool = False) -> dict:
        conn = self.discover()
        if conn is not None:
            payload: dict[str, Any] = {
                "mode": mode,
                "address": address,
                "name": name,
                "keep_others": bool(keep_others),
                "source": "agent",
            }
            if x is not None:
                payload["x"] = x
            if y is not None:
                payload["y"] = y
            if actions is not None:
                payload["actions"] = actions
            return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/targets/switch",
                                payload, server=conn)
        store = AccountStore(self.data_dir)
        try:
            account = store.get(account_id or store.selected)
        except ValueError as exc:
            raise ControlError(str(exc), code="account_not_found", http_status=404) from exc
        profile = account.load_profile()
        try:
            updated, chosen = switch_primary_target(
                profile,
                mode=mode,
                address=address,
                name=name,
                x=x,
                y=y,
                actions=actions,
                keep_others=keep_others,
                active=False,
            )
        except ValueError as exc:
            raise ControlError(str(exc), code="validation_error", http_status=400) from exc
        store.save_profile(account.id, updated)
        return {
            "account_id": account.id,
            "running": False,
            "live_applied": False,
            "source": "offline",
            "mode": mode,
            "selected_target": {
                "name": chosen.name,
                "x": chosen.x,
                "y": chosen.y,
                "actions": list(chosen.actions),
                "enabled": chosen.enabled,
            },
            "targets": summarize_targets(updated),
            "targets_brief": format_targets_brief(updated),
        }

    def set_backup(self, *, account_id: str = "", mode: str, team_key: str = "", address: str = "",
                   name: str = "", x: Optional[int] = None, y: Optional[int] = None,
                   clear: bool = False, enabled: Optional[bool] = None) -> dict:
        conn = self.discover()
        if conn is not None:
            payload: dict[str, Any] = {
                "mode": mode,
                "team_key": team_key,
                "address": address,
                "name": name,
                "clear": bool(clear),
                "source": "agent",
            }
            if x is not None:
                payload["x"] = x
            if y is not None:
                payload["y"] = y
            if enabled is not None:
                payload["enabled"] = enabled
            return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/backups",
                                payload, server=conn)
        store = AccountStore(self.data_dir)
        try:
            account = store.get(account_id or store.selected)
        except ValueError as exc:
            raise ControlError(str(exc), code="account_not_found", http_status=404) from exc
        profile = account.load_profile()
        try:
            updated = set_backup_point(
                profile,
                mode=mode,
                team_key=team_key,
                address=address,
                name=name,
                x=x,
                y=y,
                clear=clear,
                enabled=enabled,
                active=False,
            )
        except ValueError as exc:
            raise ControlError(str(exc), code="validation_error", http_status=400) from exc
        store.save_profile(account.id, updated)
        return {
            "account_id": account.id,
            "running": False,
            "live_applied": False,
            "source": "offline",
            "mode": mode,
            "targets": summarize_targets(updated),
            "targets_brief": format_targets_brief(updated),
        }

    def status(self, account_id: str = "") -> dict:
        return self.request("GET", f"/v1/accounts/{self._account_segment(account_id)}/status")

    def start(self, account_id: str = "", *, command: str = "run", execute: bool = True,
              max_orders: int = 0, duration: int = 0) -> dict:
        return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/start", {
            "command": command,
            "execute": bool(execute),
            "max_orders": int(max_orders),
            "duration": int(duration),
        })

    def stop(self, account_id: str = "") -> dict:
        return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/stop")

    def pause(self, account_id: str = "") -> dict:
        return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/pause")

    def resume(self, account_id: str = "") -> dict:
        return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/resume")

    def wake(self, account_id: str = "") -> dict:
        return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/wake")

    def refresh(self, account_id: str = "") -> dict:
        return self.request("POST", f"/v1/accounts/{self._account_segment(account_id)}/refresh")

    def logs(self, account_id: str = "", *, limit: int = 50) -> dict:
        return self.request("GET", f"/v1/accounts/{self._account_segment(account_id)}/logs?limit={int(limit)}")


def build_ctl_parser() -> argparse.ArgumentParser:
    # 公共参数既允许写在 `ctl --data-dir ... <cmd>`，也允许写在 `ctl <cmd> --data-dir ...`。
    # 使用 argparse.SUPPRESS 防止子解析器默认值覆盖主解析器已经接收到的公共参数。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS, help="数据目录路径")
    common.add_argument("--account-id", default=argparse.SUPPRESS, help="指定账号 ID；默认使用当前选中账号")

    parser = argparse.ArgumentParser(
        prog="sanmou-assistant ctl",
        description="三谋助手运行时控制与热换坐标接口（统一 JSON 输出，面向 Agent 与自动化脚本）",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("accounts", parents=[common], help="列出全部账号与当前运行状态")
    sub.add_parser("status", parents=[common], help="查看指定账号实时运行状态、队伍与目标")
    sub.add_parser("targets", parents=[common], help="查看武勋/拉线目标与备用征兵点")

    p_switch = sub.add_parser("switch-target", parents=[common], help="热切换拉线或武勋首选目标坐标")
    p_switch.add_argument("--mode", choices=["line", "merit"], required=True, help="目标类型：line(拉线) 或 merit(武勋)")
    p_switch.add_argument("--address", default="", help="游戏分享地址，例如 涪县(300,300)")
    p_switch.add_argument("--name", default="", help="地点名称备注（可选）")
    p_switch.add_argument("--x", type=int, default=None, help="X 坐标（不传 --address 时必填）")
    p_switch.add_argument("--y", type=int, default=None, help="Y 坐标（不传 --address 时必填）")
    p_switch.add_argument("--actions", default=None, help="指令顺序，逗号分隔，如 guard,attack,march 或 驻守,攻城,行军")
    p_switch.add_argument("--keep-others", action="store_true", help="保留列表中其他目标的原启用状态（默认停用旧目标）")

    p_backup = sub.add_parser("set-backup", parents=[common], help="热更新武勋备用征兵点或拉线按队征兵点")
    p_backup.add_argument("--mode", choices=["line", "merit"], required=True, help="征兵点类型：line 或 merit")
    p_backup.add_argument("--team", default="", help="拉线队伍键，例如 main:1（或 all 表示全部拉线队）")
    p_backup.add_argument("--address", default="", help="游戏分享地址，例如 营帐(634,266)")
    p_backup.add_argument("--name", default="", help="地点名称备注（可选）")
    p_backup.add_argument("--x", type=int, default=None, help="X 坐标")
    p_backup.add_argument("--y", type=int, default=None, help="Y 坐标")
    p_backup.add_argument("--clear", action="store_true", help="清空对应征兵点")
    toggle = p_backup.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true", help="启用武勋备用征兵开关（仅 mode=merit）")
    toggle.add_argument("--disable", action="store_true", help="关闭武勋备用征兵开关（仅 mode=merit）")

    p_start = sub.add_parser("start", parents=[common], help="在运行中的控制服务内启动账号任务")
    p_start.add_argument("--command", default="run",
                         choices=["run", "scan", "return", "farming", "dailies", "calibrate", "rates", "review", "preview"])
    p_start.add_argument("--execute", action="store_true", help="执行真实操作（不传则仅监控/试运行）")
    p_start.add_argument("--max-orders", type=int, default=0, help="限制本次新出征数量")
    p_start.add_argument("--duration", type=int, default=0, help="限定运行时间（秒）")

    sub.add_parser("stop", parents=[common], help="安全停止账号任务")
    sub.add_parser("pause", parents=[common], help="暂停运行中的任务（保留连接与初始化状态）")
    sub.add_parser("resume", parents=[common], help="恢复已暂停的任务并立即唤醒")
    sub.add_parser("wake", parents=[common], help="跳过巡检等待，立即开始下一轮")
    sub.add_parser("refresh", parents=[common], help="请求刷新画面与资源")

    p_logs = sub.add_parser("logs", parents=[common], help="获取账号近期实时日志")
    p_logs.add_argument("--limit", type=int, default=50, help="返回日志条数（默认 50，最大 200）")
    return parser


def _print_json(payload: dict) -> None:
    if sys.stdout is not None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def run_ctl(argv: list[str]) -> int:
    parser = build_ctl_parser()
    args = parser.parse_args(argv)
    data_dir = getattr(args, "data_dir", None) or data_directory()
    client = ControlClient(data_dir)
    aid = getattr(args, "account_id", "") or ""
    try:
        if args.action == "accounts":
            data = client.list_accounts()
        elif args.action == "status":
            data = client.status(aid)
        elif args.action == "targets":
            data = client.get_targets(aid)
        elif args.action == "switch-target":
            data = client.switch_target(
                account_id=aid,
                mode=args.mode,
                address=args.address,
                name=args.name,
                x=args.x,
                y=args.y,
                actions=args.actions,
                keep_others=args.keep_others,
            )
        elif args.action == "set-backup":
            enabled = True if args.enable else False if args.disable else None
            data = client.set_backup(
                account_id=aid,
                mode=args.mode,
                team_key=args.team,
                address=args.address,
                name=args.name,
                x=args.x,
                y=args.y,
                clear=args.clear,
                enabled=enabled,
            )
        elif args.action == "start":
            data = client.start(
                aid,
                command=args.command,
                execute=args.execute,
                max_orders=args.max_orders,
                duration=args.duration,
            )
        elif args.action == "stop":
            data = client.stop(aid)
        elif args.action == "pause":
            data = client.pause(aid)
        elif args.action == "resume":
            data = client.resume(aid)
        elif args.action == "wake":
            data = client.wake(aid)
        elif args.action == "refresh":
            data = client.refresh(aid)
        elif args.action == "logs":
            data = client.logs(aid, limit=args.limit)
        else:
            raise ControlError(f"未知子命令：{args.action}", code="invalid_command", http_status=400)
    except ControlError as exc:
        _print_json({"ok": False, "code": exc.code, "error": exc.message})
        return 1
    except Exception as exc:  # noqa: BLE001
        _print_json({"ok": False, "code": "internal_error", "error": str(exc)})
        return 1
    _print_json({"ok": True, "data": data})
    return 0
