"""grayroll-bugfix-01 争用失败测：双会话抢开关键创建锁，无锁则双成（盯锁）。

两路会话同时打同一开关键创建写口：
- 修复前：无创建锁，两笔 INSERT 均 200，库内双行且无争用痕 → 红。
- 修复后：一开关键创建锁串行化，胜方 200 正式落库，败方 409 只记 contention_marks，
  锁释放干净，败方零正式改。
"""
from __future__ import annotations

import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from app import app
from db import DB_PATH

KEY = "bf01.zheng.race"
LOCK_KEY = f"flag-create:{KEY}"
WINNER = "admin"


def _login(client, username: str, password: str) -> dict:
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _cleanup() -> None:
    with sqlite3.connect(DB_PATH) as c:
        ids = [r[0] for r in c.execute("SELECT id FROM flags WHERE key=?", (KEY,))]
        if ids:
            placeholders = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM flag_rules WHERE flag_id IN ({placeholders})", ids)
        c.execute("DELETE FROM flags WHERE key=?", (KEY,))
        c.execute("DELETE FROM audit_log WHERE entity='flag' AND detail=?", (KEY,))
        c.execute("DELETE FROM change_history WHERE detail LIKE ?", (f"%{KEY}%",))
        c.execute("DELETE FROM contention_marks WHERE lock_key=?", (LOCK_KEY,))
        c.execute("DELETE FROM row_locks WHERE lock_key=?", (LOCK_KEY,))
        c.commit()


def test_zheng_two_sessions_race_create_lock_one_winner():
    _cleanup()
    try:
        # 预置“胜方刚抢到创建锁、尚未提交”的锁态：两请求仍同时发出，
        # 败方确定性命中锁争用分支（409 + contention_marks），而非退化成唯一校验 400。
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)",
                (LOCK_KEY, WINNER, time.time() + 20.0),
            )
            c.commit()

        outcomes: list[dict] = []
        outcomes_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(username: str, password: str) -> None:
            with TestClient(app) as client:
                headers = _login(client, username, password)
                barrier.wait()
                r = client.post(
                    "/api/flags",
                    headers=headers,
                    json={"key": KEY, "title": "争用撞键夹具开关", "description": "bf01", "owner": username},
                )
                with outcomes_lock:
                    outcomes.append({"user": username, "status": r.status_code, "body": r.json()})

        t1 = threading.Thread(target=worker, args=("admin", "admin123"))
        t2 = threading.Thread(target=worker, args=("writer", "writer123"))
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        statuses = sorted(o["status"] for o in outcomes)
        # 一胜一败：200 + 409，不允许无锁双成
        assert statuses == [200, 409], f"争用结果应为 [200,409]，实际 {statuses}: {outcomes}"

        with sqlite3.connect(DB_PATH) as c:
            flag_rows = c.execute("SELECT COUNT(*) FROM flags WHERE key=?", (KEY,)).fetchone()[0]
            marks = [dict(zip(("lock_key", "loser", "winner"), r))
                     for r in c.execute(
                         "SELECT lock_key, loser, winner FROM contention_marks WHERE lock_key=?",
                         (LOCK_KEY,),
                     )]
            left_locks = c.execute("SELECT COUNT(*) FROM row_locks WHERE lock_key=?", (LOCK_KEY,)).fetchone()[0]
            audit_rows = c.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='create_flag' AND detail=?", (KEY,)
            ).fetchone()[0]

        assert flag_rows == 1, f"同 key 仅允许一正式行，实际 {flag_rows}"
        assert len(marks) == 1, f"败方须留 1 条争用痕，实际 {len(marks)}"
        assert marks[0]["lock_key"] == LOCK_KEY
        # 败方只记争用痕：winner 为持锁胜方，loser 为另一会话
        assert marks[0]["winner"] == WINNER
        assert marks[0]["loser"] != marks[0]["winner"]
        assert left_locks == 0, "事务结束后创建锁须释放干净"
        assert audit_rows == 1, f"败方零正式改：create_flag 审计仅 1 条，实际 {audit_rows}"
    finally:
        _cleanup()
