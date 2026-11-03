from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def profile_location(data: Path, explicit: Path | None = None, account_id: str = ""):
    if explicit:
        if account_id:
            raise ValueError("--profile 与 --account-id 不能同时使用")
        return explicit, data / "sessions"
    from .accounts import AccountStore
    store = AccountStore(data)
    account = store.get(account_id or store.selected)
    return account.profile_path, account.sessions_path


def main():
    parser = argparse.ArgumentParser(description="三谋桌面助手")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--scan", action="store_true", help="只读扫描后退出，结果保存到指定数据目录")
    parser.add_argument("--command", choices=["scan","calibrate","preview","rates","farming","dailies","run","launch","discover","packages","review","return","runtime","pave-reset","diagnostics"])
    parser.add_argument("--output", type=Path, help="diagnostics 诊断压缩包输出路径")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--account-id", help="指定 accounts.json 中的账号；默认使用界面当前账号")
    parser.add_argument("--execute", action="store_true", help="执行已启用的任务；否则只监控")
    parser.add_argument("--max-orders", type=int, default=0, help="实机验收时限制本次新出征数量")
    parser.add_argument("--duration", type=int, default=0, help="限定循环验收时间（秒），到时停止新动作")
    parser.add_argument("--ui-proof", type=Path, help="生成全部桌面页面截图后退出，不连接游戏")
    parser.add_argument("--read-on-open", action="store_true", help="打开桌面后读取全部队伍，不运行战斗循环")
    parser.add_argument("--start-on-open", action="store_true", help="打开完整界面后按保存配置开始执行")
    args = parser.parse_args()
    from .models import Profile, data_directory
    data = args.data_dir or data_directory()
    data.mkdir(parents=True, exist_ok=True)
    from .diagnostics import install_exception_log, export_diagnostics
    install_exception_log(data)
    if args.command == "diagnostics":
        from datetime import datetime
        output = args.output or data / "feedback" / ("三谋诊断-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
        export_diagnostics(data, output)
        if sys.stdout is not None:
            print(str(output.resolve()))
        return 0
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    app = QApplication(sys.argv[:1])
    app.setApplicationName("三谋助手")
    if args.scan or args.command:
        from .worker import Worker
        from datetime import datetime
        try:
            path, session_root = profile_location(data, args.profile, args.account_id or "")
        except (ValueError, StopIteration) as exc:
            parser.error(str(exc) or "指定账号不存在")
        profile = Profile.from_dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else Profile()
        command = args.command or "scan"
        errors = profile.validate(active=args.execute and command=="run")
        if errors:
            parser.error("；".join(errors))
        folder = session_root / datetime.now().strftime("%Y%m%d-%H%M%S")
        results = []
        def event(kind,value):
            if kind == "preview":
                return
            results.append({"event":kind,"value":value})
            if sys.stdout is not None:
                print(json.dumps(results[-1],ensure_ascii=False),flush=True)
        worker = Worker(profile,folder,command,not args.execute,args.max_orders,args.duration)
        worker.event.connect(event)
        worker.completed.connect(app.quit)
        worker.start()
        app.exec()
        worker.wait()
        (folder / "result.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
        return 1 if any(r["event"]=="error" for r in results) else 0
    from .ui import AccountManagerWindow
    window = AccountManagerWindow(data)
    app.main_window = window
    window.show()
    if args.start_on_open:
        QTimer.singleShot(600,lambda:window.start("run"))
    elif args.read_on_open:
        QTimer.singleShot(600,lambda:window.start("scan"))
    if args.ui_proof:
        args.ui_proof.mkdir(parents=True,exist_ok=True)
        def capture():
            selected_index = window.account_stack.currentIndex()
            for account_index, account_window in enumerate(window.windows):
                window.account_combo.setCurrentIndex(account_index)
                for i in range(account_window.stack.count()):
                    account_window.nav.setCurrentRow(i)
                    app.processEvents()
                    name = f"page-{i}.png" if account_index == 0 else f"account-{account_index+1}-page-{i}.png"
                    window.grab().save(str(args.ui_proof / name))
            window.account_combo.setCurrentIndex(selected_index)
            app.quit()
        QTimer.singleShot(500,capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
