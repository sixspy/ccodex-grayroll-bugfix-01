"""grayroll-bugfix-01 失败测乙：求值页把双行开关当单键齐套假绿（盯求值服务，另一模块）。

夹具：绕过写口直接入库同 key 两行正式开关（模拟缺陷已造成的脏状态），
并给其中一行挂正式规则，使旧逻辑 fetchone 静默命中返回 200 假绿。
修复后期望：求值服务联查 flags × flag_rules 发现撞键歧义，拒决求值（非 2xx）。
"""
from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient

from app import app
from db import DB_PATH

KEY = "bf01.yi.dup"


def _headers(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _cleanup() -> None:
    with sqlite3.connect(DB_PATH) as c:
        ids = [r[0] for r in c.execute("SELECT id FROM flags WHERE key=?", (KEY,))]
        if ids:
            placeholders = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM flag_rules WHERE flag_id IN ({placeholders})", ids)
        c.execute("DELETE FROM flags WHERE key=?", (KEY,))
        c.commit()


def test_yi_evaluate_refuses_ambiguous_duplicate_flag_key():
    _cleanup()
    try:
        now = time.time()
        with sqlite3.connect(DB_PATH) as c:
            # 同 key 第二笔仍入库夹具：双正式行
            cur1 = c.execute(
                "INSERT INTO flags(key, title, description, owner, status, created_at) VALUES(?,?,?,?,?,?)",
                (KEY, "乙路撞键夹具A", "bf01", "writer", "active", now),
            )
            id_a = cur1.lastrowid
            cur2 = c.execute(
                "INSERT INTO flags(key, title, description, owner, status, created_at) VALUES(?,?,?,?,?,?)",
                (KEY, "乙路撞键夹具B", "bf01", "writer", "draft", now + 1),
            )
            id_b = cur2.lastrowid
            # 挂一条正式规则，旧求值逻辑会对其中一行给出齐套假绿
            c.execute(
                "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                "VALUES(?,?,?,?,?,1,1,?)",
                (id_a, 1, "percent", 50, None, now),
            )
            c.commit()
            assert c.execute("SELECT COUNT(*) FROM flags WHERE key=?", (KEY,)).fetchone()[0] == 2

        with TestClient(app) as client:
            headers = _headers(client)
            ev = client.post(
                "/api/evaluate",
                headers=headers,
                json={"flag_key": KEY, "env_code": "dev", "subject_key": "u-bf01-yi"},
            )
            # 双行歧义不得当单键齐套假绿：必须拒决
            assert ev.status_code >= 400, f"撞键歧义应拒决求值，实际 {ev.status_code}: {ev.text}"
            assert "enabled" not in ev.json(), "拒决响应不得携带 enabled 假绿结果"

            # 总览板须暴露撞键告警（五页同源），而非静默展示
            board = client.get("/api/evaluate/board", headers=headers)
            assert board.status_code == 200
            collisions = {item["key"]: item["c"] for item in board.json().get("collisions", [])}
            assert collisions.get(KEY) == 2
    finally:
        _cleanup()
