"""grayroll-bugfix-01 失败测甲：同 key 第二笔仍写正式开关行（盯开关服务写口）。

口径（甲路径）：撞键第二笔正式零增拒写——HTTP 拒写且 flags/audit/history 均零增。
本文件在修复前必须见红：第二笔当前返回 200 且库内双行。
"""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app import app
from db import DB_PATH

KEY = "bf01.jia.collide"
LOCK_KEY = f"flag-create:{KEY}"


def _headers(client, username: str, password: str):
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


def test_jia_same_key_second_write_rejected_zero_growth():
    _cleanup()
    try:
        with TestClient(app) as client:
            headers = _headers(client, "admin", "admin123")
            payload = {"key": KEY, "title": "甲路撞键夹具开关", "description": "bf01", "owner": "writer"}

            first = client.post("/api/flags", headers=headers, json=payload)
            assert first.status_code == 200, first.text

            second = client.post("/api/flags", headers=headers, json=payload)
            # 撞键第二笔必须拒写（400），不得再返回 200
            assert second.status_code == 400, second.text

        with sqlite3.connect(DB_PATH) as c:
            flag_rows = c.execute("SELECT COUNT(*) FROM flags WHERE key=?", (KEY,)).fetchone()[0]
            audit_rows = c.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='create_flag' AND detail=?", (KEY,)
            ).fetchone()[0]
            history_rows = c.execute(
                "SELECT COUNT(*) FROM change_history WHERE action='create_flag' AND detail LIKE ?",
                (f"%{KEY}%",),
            ).fetchone()[0]

        # 同 key 仅一正式行；拒写侧零正式痕迹
        assert flag_rows == 1, f"同 key 正式开关行应为 1，实际 {flag_rows}"
        assert audit_rows == 1, f"create_flag 审计应仅 1 条，实际 {audit_rows}"
        assert history_rows == 1, f"create_flag 历史应仅 1 条，实际 {history_rows}"
    finally:
        _cleanup()
