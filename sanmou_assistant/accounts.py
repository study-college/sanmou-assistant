from __future__ import annotations

import copy
import json
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .models import Profile, atomic_json


@dataclass(frozen=True)
class AccountContext:
    id: str
    name: str
    root: Path

    @property
    def profile_path(self) -> Path:
        return self.root / "profile.json"

    @property
    def sessions_path(self) -> Path:
        return self.root / "sessions"

    def load_profile(self) -> Profile:
        return Profile.from_dict(json.loads(self.profile_path.read_text(encoding="utf-8")))


class AccountStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.data_dir / "accounts.json"
        self.migration_error = ""
        self.load_errors = []
        self._unavailable_accounts = []
        if self.registry_path.exists():
            self._load()
        else:
            self._migrate_legacy()

    @property
    def selected(self) -> str:
        return self._selected

    @property
    def accounts(self) -> list[AccountContext]:
        return list(self._accounts)

    def get(self, account_id: str) -> AccountContext:
        for account in self._accounts:
            if account.id == account_id:
                return account
        raise ValueError(f"账号不存在：{account_id}")

    def select(self, account_id: str) -> None:
        self.get(account_id)
        self._selected = account_id
        self._write_registry()

    def save_profile(self, account_id: str, profile: Profile) -> None:
        account = self.get(account_id)
        atomic_json(account.profile_path, profile.to_dict())
        self._accounts = [AccountContext(a.id, profile.account or profile.name, a.root)
                          if a.id == account_id else a for a in self._accounts]
        self._write_registry()

    def create_account(self, name: str, instance: str, template: Profile | None = None) -> AccountContext:
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("账号名称须为 1～80 个字符")
        if not str(instance).isdigit():
            raise ValueError("请选择 MuMu 实例编号")
        account_id = "account-" + uuid.uuid4().hex[:12]
        root = self.data_dir / "accounts" / account_id
        profile = Profile.from_dict(copy.deepcopy(template.to_dict())) if template else Profile()
        profile.name = name
        profile.account = ""
        profile.main_city = None
        profile.instance = str(instance)
        errors = profile.validate()
        if errors:
            raise ValueError("；".join(errors))
        atomic_json(root / "profile.json", profile.to_dict())
        account = AccountContext(account_id, name, root)
        previous_selected = self._selected
        self._accounts.append(account)
        self._selected = account_id
        try:
            self._write_registry()
        except Exception:
            self._accounts.pop()
            self._selected = previous_selected
            shutil.rmtree(root, ignore_errors=True)
            raise
        return account

    def _load(self) -> None:
        raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != 1 or not isinstance(raw.get("accounts"), list):
            raise ValueError("账号列表格式或版本不正确")
        accounts = []
        unavailable = []
        for row in raw["accounts"]:
            if not isinstance(row, dict) or not re.fullmatch(r"account-[a-f0-9]{12}", str(row.get("id", ""))):
                raise ValueError("账号列表包含无效账号标识")
            account_id = str(row["id"])
            root = self.data_dir / "accounts" / account_id
            if not (root / "profile.json").is_file():
                unavailable.append(row)
                self.load_errors.append(f"账号配置缺失：{account_id}；已跳过该账号并保留原记录")
                continue
            accounts.append(AccountContext(account_id, str(row.get("name") or account_id), root))
        if not accounts or len({a.id for a in accounts}) != len(accounts):
            detail = "；".join(self.load_errors)
            raise ValueError("账号列表为空、全部不可用或包含重复账号" + (f"：{detail}" if detail else ""))
        self._unavailable_accounts = unavailable
        selected = str(raw.get("selected") or accounts[0].id)
        if selected not in {a.id for a in accounts}:
            selected = accounts[0].id
        self._accounts = accounts
        self._selected = selected

    def _migrate_legacy(self) -> None:
        path = self.data_dir / "active-profile.json"
        profile = Profile()
        if path.exists():
            try:
                profile = Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                self.migration_error = f"原配置无法读取，已保留原文件：{exc}"
        account_id = "account-" + uuid.uuid4().hex[:12]
        root = self.data_dir / "accounts" / account_id
        atomic_json(root / "profile.json", profile.to_dict())
        self._accounts = [AccountContext(account_id, profile.account or profile.name, root)]
        self._selected = account_id
        try:
            legacy_database = self.data_dir / "sessions" / "runtime.sqlite"
            database = root / "sessions" / "runtime.sqlite"
            if legacy_database.is_file() and not database.exists():
                database.parent.mkdir(parents=True, exist_ok=True)
                source = sqlite3.connect(legacy_database.as_uri() + "?mode=ro", uri=True)
                try:
                    target = sqlite3.connect(database)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                finally:
                    source.close()
            self._write_registry()
        except Exception:
            self._accounts = []
            self._selected = ""
            shutil.rmtree(root, ignore_errors=True)
            raise

    def _write_registry(self) -> None:
        available = [{"id": account.id, "name": account.name} for account in self._accounts]
        unavailable = [{"id": str(row["id"]), "name": str(row.get("name") or row["id"])}
                       for row in self._unavailable_accounts]
        atomic_json(self.registry_path, {
            "schema": 1,
            "selected": self._selected,
            "accounts": available + unavailable,
        })
