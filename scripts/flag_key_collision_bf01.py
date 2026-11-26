#!/usr/bin/env python3
"""grayroll-bugfix-01 第1闩：并发双请求打争用写口（开关键创建锁）。

两路会话（admin / writer）同时对同一开关键发起创建：
  胜方 200 正式落库；败方 409 仅留 contention_marks 争用痕，零正式改。

两种运行模式：
  1) 默认：进程内 TestClient + 线程屏障（无需起服务，CI 可直接跑）
  2) 在线：python flag_key_collision_bf01.py --base http://127.0.0.1:8000
退出码非 0 表示验收失败。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from db import DB_PATH  # noqa: E402

KEY = "bf01.script.race"
LOCK_KEY = f"flag-create:{KEY}"
WINNER = "admin"
USERS = [("admin", "admin123"), ("writer", "writer123")]


def cleanup() -> None:
    with sqlite3.connect(DB_PATH) as c:
        ids = [r[0] for r in c.execute("SELECT id FROM flags WHERE key=?", (KEY,))]
        if ids:
            ph = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM flag_rules WHERE flag_id IN ({ph})", ids)
        c.execute("DELETE FROM flags WHERE key=?", (KEY,))
        c.execute("DELETE FROM audit_log WHERE entity='flag' AND detail=?", (KEY,))
        c.execute("DELETE FROM change_history WHERE detail LIKE ?", (f"%{KEY}%",))
        c.execute("DELETE FROM contention_marks WHERE lock_key=?", (LOCK_KEY,))
        c.execute("DELETE FROM row_locks WHERE lock_key=?", (LOCK_KEY,))
        c.commit()


def seed_winner_lock() -> None:
    """预置胜方持锁态，令并发败方确定性撞创建锁（409 + 争用痕）。"""
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)",
            (LOCK_KEY, WINNER, time.time() + 20.0),
        )
        c.commit()


def _login_local(client, username: str, password: str) -> str:
    r = client.post("/api/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["token"]


def run_inprocess() -> list[dict]:
    from fastapi.testclient import TestClient

    from app import app

    results: list[dict] = []
    box = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(username: str, password: str) -> None:
        with TestClient(app) as client:
            token = _login_local(client, username, password)
            barrier.wait()
            r = client.post(
                "/api/flags",
                headers={"Authorization": f"Bearer {token}"},
                json={"key": KEY, "title": "并发脚本撞键开关", "description": "bf01", "owner": username},
            )
            with box:
                results.append({"user": username, "status": r.status_code, "body": r.json()})

    threads = [threading.Thread(target=worker, args=u) for u in USERS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def run_live(base: str) -> list[dict]:
    import httpx

    results: list[dict] = []
    box = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(username: str, password: str) -> None:
        with httpx.Client(base_url=base, timeout=10) as client:
            lr = client.post("/api/login", json={"username": username, "password": password})
            lr.raise_for_status()
            token = lr.json()["token"]
            barrier.wait()
            r = client.post(
                "/api/flags",
                headers={"Authorization": f"Bearer {token}"},
                json={"key": KEY, "title": "并发脚本撞键开关", "description": "bf01", "owner": username},
            )
            with box:
                results.append({"user": username, "status": r.status_code, "body": r.json()})

    threads = [threading.Thread(target=worker, args=u) for u in USERS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def verify(results: list[dict]) -> bool:
    ok = True
    statuses = sorted(r["status"] for r in results)
    print("双请求结果：")
    for r in results:
        print(f"  user={r['user']:<7} status={r['status']} body={json.dumps(r['body'], ensure_ascii=False)}")
    if statuses != [200, 409]:
        print(f"[FAIL] 争用结果应为 [200,409]，实际 {statuses}（无锁双成或全败）")
        ok = False

    with sqlite3.connect(DB_PATH) as c:
        flag_rows = c.execute("SELECT COUNT(*) FROM flags WHERE key=?", (KEY,)).fetchone()[0]
        marks = c.execute(
            "SELECT loser, winner FROM contention_marks WHERE lock_key=?", (LOCK_KEY,)
        ).fetchall()
        left_locks = c.execute("SELECT COUNT(*) FROM row_locks WHERE lock_key=?", (LOCK_KEY,)).fetchone()[0]
        audit_rows = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='create_flag' AND detail=?", (KEY,)
        ).fetchone()[0]

    print(f"库内核验：正式行={flag_rows} 争用痕={len(marks)} 残留锁={left_locks} 正式审计={audit_rows}")
    if flag_rows != 1:
        print(f"[FAIL] 同 key 正式行应为 1，实际 {flag_rows}")
        ok = False
    if len(marks) != 1 or marks[0][0] == marks[0][1]:
        print(f"[FAIL] 应有 1 条败方争用痕且胜败双方不同，实际 {marks}")
        ok = False
    if left_locks != 0:
        print("[FAIL] 创建锁未释放")
        ok = False
    if audit_rows != 1:
        print(f"[FAIL] 败方零正式改，create_flag 审计应为 1，实际 {audit_rows}")
        ok = False
    if ok:
        print("[PASS] 争用核销：一胜一败，败方仅留争用痕，正式零改")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="在线服务地址，缺省走进程内 TestClient")
    args = ap.parse_args()
    cleanup()
    try:
        seed_winner_lock()
        results = run_live(args.base) if args.base else run_inprocess()
        ok = verify(results)
    finally:
        cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
