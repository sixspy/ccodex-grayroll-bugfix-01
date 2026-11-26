"""gbf01 争用失败测 — 双会话抢「环境编码创建互斥」，禁止无锁双成。

与 grayroll-bugfix-01 家族同一验收口径：
- 预置胜方 admin 持 env-create:{code} 未过期锁，admin/writer 双请求屏障齐发；
- 结果必须恰为 [200,409]：胜方正式落库并释放锁，败方只在 contention_marks 留 1 条痕；
- 正式环境册同编码仅 1 行，create_env 审计仅 1 条，锁释放干净。
另有不预植锁的真实并发 duel_env_code_unique，靠 DB UNIQUE 兜底保证绝不双行。

种子代码没有 POST /api/envs 也没有环境编码创建互斥，本文件先见红（两笔 405）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import db
from app import app
from envs_svc import env_create_lock_key


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "gbf01_race.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path


def _seed_winner_lock(db_path, code: str, winner: str = "admin", ttl: float = 20.0) -> str:
    lock_key = env_create_lock_key(code)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)",
            (lock_key, winner, time.time() + ttl),
        )
        conn.commit()
    finally:
        conn.close()
    return lock_key


def duel_env_code_unique(code: str, db_path=None, seed_winner: bool = True):
    """并发双请求争抢同一环境编码，返回两笔结果 (user, status)。

    seed_winner=True 时预置 admin 持锁，确定性地打出 [200,409] 争用核销。
    """
    if seed_winner and db_path is not None:
        _seed_winner_lock(db_path, code, "admin")

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, int]] = []
    box = threading.Lock()

    def worker(username: str, password: str, title: str):
        with TestClient(app) as client:
            token = client.post(
                "/api/login", json={"username": username, "password": password}
            ).json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            barrier.wait(timeout=10)
            r = client.post(
                "/api/envs",
                headers=headers,
                json={"code": code, "title": title, "risk_level": 2},
            )
            with box:
                outcomes.append((username, r.status_code))

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(worker, "admin", "admin123", "决斗笔 A")
        f2 = pool.submit(worker, "writer", "writer123", "决斗笔 B")
        f1.result(timeout=30)
        f2.result(timeout=30)
    return outcomes


def test_duel_env_code_unique(isolated_db, monkeypatch):
    db_path = isolated_db
    code = "duel-unique-east"
    lock_key = env_create_lock_key(code)
    _seed_winner_lock(db_path, code, "admin")

    # 确定性交错闸门：胜方 admin 在 finally 释放锁前，先等败方 contention_mark 落库，
    # 从而固定败方一定命中 409 争用分支（而非调度过快退化成唯一校验 400）。
    import envs_svc

    real_release = envs_svc.release

    def gated_release(conn, key, holder):
        if key == lock_key and holder == "admin":
            for _ in range(1000):
                probe = sqlite3.connect(db_path)
                try:
                    n = probe.execute(
                        "SELECT COUNT(*) c FROM contention_marks WHERE lock_key=?", (key,)
                    ).fetchone()[0]
                finally:
                    probe.close()
                if n >= 1:
                    break
                time.sleep(0.01)
        return real_release(conn, key, holder)

    monkeypatch.setattr(envs_svc, "release", gated_release)

    outcomes = duel_env_code_unique(code, None, seed_winner=False)
    statuses = sorted(s for _, s in outcomes)
    assert statuses == [200, 409], f"预置胜方锁后应 [200,409]，实际 {outcomes}"

    conn = sqlite3.connect(db_path)
    try:
        env_n = conn.execute(
            "SELECT COUNT(*) c FROM environments WHERE code=?", (code,)
        ).fetchone()[0]
        marks = conn.execute(
            "SELECT loser, winner FROM contention_marks WHERE lock_key=?", (lock_key,)
        ).fetchall()
        left_locks = conn.execute(
            "SELECT COUNT(*) c FROM row_locks WHERE lock_key=?", (lock_key,)
        ).fetchone()[0]
        audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action='create_env' AND detail=?", (code,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert env_n == 1, "争用后正式环境册同编码只能一行"
    assert len(marks) == 1, f"败方须恰留 1 条争用痕，实际 {marks}"
    assert marks[0][1] == "admin"
    assert marks[0][0] != marks[0][1]
    assert left_locks == 0, "胜方事务结束后创建锁须释放干净"
    assert audits == 1, "败方零正式改：create_env 审计仅胜方 1 条"


def test_foreign_held_lock_makes_loser_leave_only_mark(isolated_db):
    db_path = isolated_db
    code = "held-lock-east"
    lock_key = _seed_winner_lock(db_path, code, winner="rival-winner-session", ttl=600)

    with TestClient(app) as c:
        token = c.post(
            "/api/login", json={"username": "writer", "password": "writer123"}
        ).json()["token"]
        r = c.post(
            "/api/envs",
            headers={"Authorization": f"Bearer {token}"},
            json={"code": code, "title": "抢锁失败笔", "risk_level": 2},
        )

    assert r.status_code == 409, f"他会话持锁必须 409，实际 {r.status_code}: {r.text}"

    conn = sqlite3.connect(db_path)
    try:
        env_n = conn.execute(
            "SELECT COUNT(*) c FROM environments WHERE code=?", (code,)
        ).fetchone()[0]
        mark = conn.execute(
            "SELECT loser, winner FROM contention_marks WHERE lock_key=? ORDER BY id DESC LIMIT 1",
            (lock_key,),
        ).fetchone()
        audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action='create_env' AND detail=?", (code,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert env_n == 0
    assert audits == 0
    assert mark is not None and mark[0] == "writer" and mark[1] == "rival-winner-session"


def test_unseeded_duel_never_double_writes(isolated_db):
    """不预植锁的真实并发：唯一约束兜底，恰一笔成、库内一行，绝不无锁双成。"""
    db_path = isolated_db
    code = "free-duel-east"
    outcomes = duel_env_code_unique(code, db_path, seed_winner=False)

    successes = [x for x in outcomes if 200 <= x[1] < 300]
    assert len(successes) == 1, f"同编码并发必须恰一笔成功，实际 {outcomes}"
    assert all(s in (200, 400, 409) for _, s in outcomes), outcomes

    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM environments WHERE code=?", (code,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1
