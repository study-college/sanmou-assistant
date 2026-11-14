from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path


def cycle_key(task: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if task in ("farming", "drill", "bandits"):
        now -= timedelta(hours=7)
    return now.strftime("%Y-%m-%d")


class Journal:
    """单工作线程持有连接；下单前落盘，崩溃后不自动重发未确认命令。"""

    def __init__(self, path: Path, scope: str):
        self.db = sqlite3.connect(path)
        self.scope = scope
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, scope TEXT, team TEXT, kind TEXT, target TEXT, status TEXT, created REAL, note TEXT);
            CREATE TABLE IF NOT EXISTS phases(scope TEXT, team TEXT, phase TEXT, PRIMARY KEY(scope,team));
            CREATE TABLE IF NOT EXISTS daily(scope TEXT, task TEXT, cycle TEXT, status TEXT, count INTEGER, PRIMARY KEY(scope,task,cycle));
            CREATE TABLE IF NOT EXISTS flags(scope TEXT, name TEXT, value INTEGER, PRIMARY KEY(scope,name));
            CREATE TABLE IF NOT EXISTS task_context(scope TEXT, task TEXT, cycle TEXT, data TEXT, PRIMARY KEY(scope,task,cycle));
            CREATE TABLE IF NOT EXISTS pave(scope TEXT, route TEXT, idx INTEGER, status TEXT, note TEXT, updated REAL,
                fingerprint TEXT, origin TEXT, intent TEXT, PRIMARY KEY(scope,route));
        """)
        # 0.3.2 已建过没有指纹/出发点的旧表：补列，保留原进度。
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(pave)")}
        for name in ("fingerprint", "origin", "intent"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE pave ADD COLUMN {name} TEXT")
        self.db.commit()

    def pending(self, team: str):
        """已写订单但还没结清的命令：prepared 表示提交中，uncertain 表示没读到受理证据。

        普通出征在确认不到受理时会当场结清为 rejected 并允许下一轮重试；
        讨贼、回城和铺路的 uncertain 先按现场核对再决定：队伍已行动/到达按 accepted 结清，
        冷却后确认仍在主城（加入未提交、次数未消耗）按 rejected 结清，避免消耗次数或永久锁队。
        """
        return self.db.execute("SELECT id,kind,target,status,created FROM intents WHERE scope=? AND team=? AND status IN ('prepared','uncertain') ORDER BY created DESC", (self.scope, team)).fetchall()

    def recent_orders(self, team: str):
        """仅供界面/诊断：该队最近的命令记录，含刚结清的受理结果。"""
        return self.db.execute("SELECT id,kind,target,status,created FROM intents WHERE scope=? AND team=? ORDER BY created DESC LIMIT 20", (self.scope, team)).fetchall()

    def prepare(self, team: str, kind: str, target: str) -> str:
        if self.pending(team):
            raise RuntimeError(f"{team} 有待核对的命令，禁止重复下单")
        ident = uuid.uuid4().hex
        self.db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?)", (ident, self.scope, team, kind, target, "prepared", time.time(), ""))
        self.db.commit()
        return ident

    def finish(self, ident: str, status: str, note: str = ""):
        if status not in ("accepted", "rejected", "uncertain", "reviewed"):
            raise ValueError(status)
        self.db.execute("UPDATE intents SET status=?,note=? WHERE id=? AND scope=?", (status, note, ident, self.scope))
        self.db.commit()

    def phase(self, team: str) -> str:
        row = self.db.execute("SELECT phase FROM phases WHERE scope=? AND team=?", (self.scope, team)).fetchone()
        return row[0] if row else ""

    def pave_progress(self, route: str) -> dict:
        """路线进度；dispatching 表示已写订单但结果未确认，running 表示等待到达。"""
        row = self.db.execute(
            "SELECT idx,status,note,fingerprint,origin,intent,updated FROM pave WHERE scope=? AND route=?",
            (self.scope, route)).fetchone()
        if not row:
            return {"idx": 0, "status": "", "note": "", "fingerprint": "", "origin": "", "intent": "", "updated": 0.0}
        return {"idx": row[0], "status": row[1] or "", "note": row[2] or "",
                "fingerprint": row[3] or "", "origin": row[4] or "", "intent": row[5] or "",
                "updated": row[6] or 0.0}

    def set_pave(self, route: str, idx: int, status: str, note: str = "",
                 *, fingerprint=None, origin=None, intent=None):
        current = self.pave_progress(route)
        self.db.execute("INSERT OR REPLACE INTO pave VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.scope, route, int(idx), status, note, time.time(),
                         current["fingerprint"] if fingerprint is None else fingerprint,
                         current["origin"] if origin is None else origin,
                         current["intent"] if intent is None else intent))
        self.db.commit()

    def begin_pave(self, team: str, route: str, idx: int, target: str,
                   origin: str, fingerprint: str) -> str:
        """原子写入订单和路线进度：进程在提交前后退出都能据此核对，不会盲目重发。"""
        if self.pending(team):
            raise RuntimeError(f"{team} 有待核对的命令，禁止重复下单")
        ident = uuid.uuid4().hex
        self.db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?)",
                        (ident, self.scope, team, "pave", target, "prepared", time.time(), ""))
        self.db.execute("INSERT OR REPLACE INTO pave VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.scope, route, int(idx), "dispatching", target, time.time(),
                         fingerprint, origin, ident))
        self.db.commit()
        return ident

    def mark_pave_unknown(self, ident: str, route: str, idx: int, note: str = ""):
        """提交结果未知：订单留待核对，路线保持 dispatching，不重发。"""
        self.db.execute("UPDATE intents SET status='uncertain',note=? WHERE id=? AND scope=?",
                        (note, ident, self.scope))
        self.db.execute("UPDATE pave SET idx=?,status='dispatching',note=?,updated=? WHERE scope=? AND route=?",
                        (int(idx), note, time.time(), self.scope, route))
        self.db.commit()

    def settle_pave(self, ident: str, status: str, route: str, idx: int,
                    route_status: str, note: str = ""):
        if status not in ("accepted", "rejected", "uncertain", "reviewed"):
            raise ValueError(status)
        self.db.execute("UPDATE intents SET status=?,note=? WHERE id=? AND scope=?",
                        (status, note, ident, self.scope))
        self.db.execute("UPDATE pave SET idx=?,status=?,note=?,intent='',updated=? WHERE scope=? AND route=?",
                        (int(idx), route_status, note, time.time(), self.scope, route))
        self.db.commit()

    def failure_streak(self, team: str) -> int:
        rows = self.db.execute("SELECT status FROM intents WHERE scope=? AND team=? AND status IN ('accepted','rejected','reviewed') ORDER BY created DESC LIMIT 10", (self.scope,team)).fetchall()
        count = 0
        for (status,) in rows:
            if status in ("accepted", "reviewed"):
                break
            count += 1
        return count

    def review_team(self, team: str):
        for row in self.pending(team):
            self.finish(row[0], "reviewed", "用户点击重新核对，现场确认已在主城")
        self.db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex,self.scope,team,"manual_review","主城现场核对","reviewed",time.time(),"解除连续失败限制"))
        self.db.commit()

    def set_phase(self, team: str, phase: str):
        self.db.execute("INSERT OR REPLACE INTO phases VALUES(?,?,?)", (self.scope, team, phase))
        self.db.commit()

    def flag(self,name: str) -> bool:
        row = self.db.execute("SELECT value FROM flags WHERE scope=? AND name=?",(self.scope,name)).fetchone()
        return bool(row[0]) if row else False

    def set_flag(self,name: str,value: bool):
        self.db.execute("INSERT OR REPLACE INTO flags VALUES(?,?,?)",(self.scope,name,int(value)))
        self.db.commit()

    def daily(self, task: str, cycle: str | None = None) -> tuple[str, int]:
        row = self.db.execute("SELECT status,count FROM daily WHERE scope=? AND task=? AND cycle=?", (self.scope, task, cycle or cycle_key(task))).fetchone()
        return row if row else ("", 0)

    def record_daily(self, task: str, status: str, increment: int = 0, cycle: str | None = None):
        cycle = cycle or cycle_key(task)
        _, count = self.daily(task, cycle)
        self.db.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?)", (self.scope, task, cycle, status, count + increment))
        self.db.commit()

    def daily_context(self, task: str, cycle: str | None = None):
        row = self.db.execute("SELECT data FROM task_context WHERE scope=? AND task=? AND cycle=?",
                              (self.scope, task, cycle or cycle_key(task))).fetchone()
        return json.loads(row[0]) if row else None

    def pending_daily(self, task: str):
        return self.db.execute("SELECT cycle FROM daily WHERE scope=? AND task=? AND status='pending' ORDER BY cycle DESC",
                               (self.scope, task)).fetchall()

    def prepare_daily(self, task: str, evidence: dict) -> str:
        """Freeze the cycle before submission; recurring rewards isolate cycles, milestones do not reset."""
        cycle = cycle_key(task)
        pending = self.pending_daily(task)
        same_cycle_pending = any(row[0] == cycle for row in pending)
        one_time_milestone = task.startswith("signin_milestone_") and pending
        if same_cycle_pending or one_time_milestone or self.daily(task, cycle)[0] == 'done':
            raise RuntimeError(f"{task} 已完成或有待核对记录，禁止重复提交")
        _, count = self.daily(task, cycle)
        data = dict(evidence, created=time.time())
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO task_context VALUES(?,?,?,?)",
                            (self.scope, task, cycle, json.dumps(data, ensure_ascii=False)))
            self.db.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?)",
                            (self.scope, task, cycle, 'pending', count))
        return cycle

    def finish_daily(self, task: str, cycle: str, evidence: dict) -> bool:
        if self.daily(task, cycle)[0] != 'pending':
            return False
        data = self.daily_context(task, cycle) or {}
        data.update(evidence, confirmed=time.time())
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO task_context VALUES(?,?,?,?)",
                            (self.scope, task, cycle, json.dumps(data, ensure_ascii=False)))
            self.db.execute("UPDATE daily SET status='done', count=count+1 WHERE scope=? AND task=? AND cycle=?",
                            (self.scope, task, cycle))
        return True

    def farming_attempt(self):
        row = self.db.execute("SELECT data FROM task_context WHERE scope=? AND task='farming' AND cycle=?", (self.scope,cycle_key("farming"))).fetchone()
        return json.loads(row[0]) if row else None

    def observe_bandit_availability(self, mode: str, remaining: int, cycle: str):
        if mode not in ("reward", "assist") or type(remaining) is not int or remaining < 0:
            raise ValueError("讨贼次数或参与方式无效")
        if mode == "assist" and remaining != 0:
            raise ValueError("协助模式不能作为正奖励次数")
        data = {"mode": mode, "remaining": remaining, "observed": time.time()}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO task_context VALUES(?,?,?,?)", (
                self.scope, "bandit_availability", cycle, json.dumps(data)))

    def bandits_exhausted(self):
        data = self.daily_context("bandit_availability", cycle_key("bandits"))
        return bool(data and data.get("remaining") == 0)

    def observe_farming_remaining(self, remaining: int, cycle: str):
        """Keep actual game availability separate from the local spending budget."""
        if not isinstance(remaining, int) or remaining < 0:
            raise ValueError("屯田剩余次数必须为非负整数")
        data = {"remaining": remaining, "observed": time.time()}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO task_context VALUES(?,?,?,?)",
                            (self.scope, "farming_remaining", cycle, json.dumps(data)))

    def farming_exhausted(self) -> bool:
        cycle = cycle_key("farming")
        observed = self.daily_context("farming_remaining", cycle)
        if observed is not None:
            return observed.get("remaining") == 0
        # 兼容升级前已经确认的最后一次结果，不必再次导航才能知道余数为0。
        attempt = self.daily_context("farming", cycle) or {}
        return (self.daily("farming", cycle)[0] == "done"
                and bool(attempt.get("confirmed")) and attempt.get("remaining_after") == 0)

    def prepare_farming(self, remaining: int, grain: int | None, income: int):
        status, count = self.daily("farming")
        if status == "pending":
            raise RuntimeError("上次屯田尚待核对，禁止重复消耗次数")
        if remaining <= 0 or income <= 0:
            raise ValueError("屯田剩余次数和粮食收益必须为正数")
        data = {"remaining_before":remaining,"grain_before":grain,"income":income,"created":time.time()}
        cycle = cycle_key("farming")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO task_context VALUES(?,?,?,?)", (self.scope,"farming",cycle,json.dumps(data)))
            self.db.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?)", (self.scope,"farming",cycle,"pending",count))

    def confirm_farming(self, remaining: int, grain: int | None) -> bool:
        status, count = self.daily("farming")
        data = self.farming_attempt()
        # 游戏次数准确减少一才确认；库存同时被征兵消耗，不要求净增达到收益。
        if status != "pending" or not data or data["remaining_before"] - remaining != 1:
            return False
        data.update(remaining_after=remaining,grain_after=grain,confirmed=time.time())
        cycle = cycle_key("farming")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO task_context VALUES(?,?,?,?)", (self.scope,"farming",cycle,json.dumps(data)))
            self.db.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?)", (self.scope,"farming",cycle,"done",count+1))
        return True

    def close(self):
        self.db.close()

