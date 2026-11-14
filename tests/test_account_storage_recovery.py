import json
import shutil

import pytest

from sanmou_assistant.accounts import AccountStore
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Profile


def test_missing_account_profile_is_skipped_without_erasing_registry_entry(tmp_path):
    store = AccountStore(tmp_path)
    first = store.accounts[0]
    second = store.create_account("账号二", "1")
    second.profile_path.unlink()

    reopened = AccountStore(tmp_path)
    assert [account.id for account in reopened.accounts] == [first.id]
    assert second.id in reopened.load_errors[0]

    reopened.save_profile(first.id, first.load_profile())
    registry = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert {row["id"] for row in registry["accounts"]} == {first.id, second.id}
    assert second.root.exists()


def test_account_manager_survives_and_surfaces_missing_profile(tmp_path):
    from PySide6.QtWidgets import QApplication
    from sanmou_assistant.ui import AccountManagerWindow
    app = QApplication.instance() or QApplication([])
    store = AccountStore(tmp_path)
    second = store.create_account("损坏账号", "1")
    second.profile_path.unlink()

    manager = AccountManagerWindow(tmp_path)
    assert len(manager.windows) == 1
    assert second.id in manager.current_window.status.text()
    manager.close()
    app.processEvents()


def test_legacy_migration_closes_sqlite_connections(tmp_path):
    (tmp_path / "sessions").mkdir()
    Journal(tmp_path / "sessions" / "runtime.sqlite", "legacy").close()
    store = AccountStore(tmp_path)

    shutil.rmtree(store.accounts[0].root)
    shutil.rmtree(tmp_path / "sessions")


def test_legacy_migration_failure_removes_only_new_account_directory(tmp_path, monkeypatch):
    original = Profile(name="原配置")
    legacy = tmp_path / "active-profile.json"
    legacy.write_text(json.dumps(original.to_dict()), encoding="utf-8")
    monkeypatch.setattr(AccountStore, "_write_registry", lambda self: (_ for _ in ()).throw(OSError("磁盘不可写")))

    with pytest.raises(OSError, match="磁盘不可写"):
        AccountStore(tmp_path)

    assert legacy.read_text(encoding="utf-8") == json.dumps(original.to_dict())
    assert not list((tmp_path / "accounts").glob("account-*"))


def test_create_account_failure_restores_selection_and_removes_new_directory(tmp_path, monkeypatch):
    store = AccountStore(tmp_path)
    first = store.accounts[0]
    monkeypatch.setattr(store, "_write_registry", lambda: (_ for _ in ()).throw(OSError("注册表失败")))

    with pytest.raises(OSError, match="注册表失败"):
        store.create_account("未完成的账号", "1")

    assert store.selected == first.id
    assert [account.id for account in store.accounts] == [first.id]
    assert list((tmp_path / "accounts").glob("account-*")) == [first.root]
