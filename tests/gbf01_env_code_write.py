"""gbf01 失败测甲 — 同编码环境第二笔必须整笔拒写，正式环境册零增。

夹具「已有同编码再创建」：环境编码表先存在 staging-east，再用同编码发起第二笔创建。
正确口径（甲路径）：第二笔整笔拒写（4xx），environments 正式册对该编码始终只有一行。

本测试在未提供 POST /api/envs 唯一约束创建口的种子代码上先见红。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
from app import app

CODE = "staging-east"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "gbf01_write.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    with TestClient(app) as c:
        yield c


def _token(c: TestClient, username: str = "admin", password: str = "admin123") -> str:
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(c: TestClient, username: str = "admin", password: str = "admin123") -> dict:
    return {"Authorization": f"Bearer {_token(c, username, password)}"}


def _code_rows(db_path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM environments WHERE code=? ORDER BY id", (CODE,)).fetchall()
    finally:
        conn.close()


def _seed_existing_code(db_path) -> int:
    """夹具前置：环境编码表里已有一行 staging-east。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO environments(code, title, risk_level) VALUES(?,?,?)",
            (CODE, "东部预发（已有）", 2),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_fixture_existing_same_code_then_create_is_rejected(client, tmp_path):
    db_path = tmp_path / "gbf01_write.db"
    existing_id = _seed_existing_code(db_path)
    h = _h(client)

    before = _code_rows(db_path)
    assert len(before) == 1

    r = client.post(
        "/api/envs",
        headers=h,
        json={"code": CODE, "title": "东部预发（撞码第二笔）", "risk_level": 2},
    )

    # 甲路径：同编码第二笔整笔拒写（家族口径精确为 400），不得回 2xx 成功。
    assert r.status_code == 400, f"同编码第二笔必须 400 拒写，实际 {r.status_code}: {r.text}"
    assert "已存在" in r.json()["detail"]

    after = _code_rows(db_path)
    assert [row["id"] for row in after] == [existing_id]
    assert len(after) == 1, f"正式环境册同编码必须只有一行，实际 {len(after)} 行"


def test_second_same_code_keeps_official_registry_zero_increment(client, tmp_path):
    """失败测甲本体：检查『同编码第二笔仍写入正式环境册』这一埋雷被封死。"""
    db_path = tmp_path / "gbf01_write.db"
    h = _h(client)

    first = client.post(
        "/api/envs",
        headers=h,
        json={"code": CODE, "title": "东部预发", "risk_level": 2},
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    conn = sqlite3.connect(db_path)
    total_before = conn.execute("SELECT COUNT(*) c FROM environments").fetchone()[0]
    code_before = conn.execute("SELECT COUNT(*) c FROM environments WHERE code=?", (CODE,)).fetchone()[0]
    max_id_before = conn.execute("SELECT MAX(id) m FROM environments").fetchone()[0]
    conn.close()
    assert code_before == 1

    second = client.post(
        "/api/envs",
        headers=h,
        json={"code": CODE, "title": "撞码第二笔", "risk_level": 3},
    )
    assert second.status_code >= 400, "同编码第二笔不得回成功"

    conn = sqlite3.connect(db_path)
    total_after = conn.execute("SELECT COUNT(*) c FROM environments").fetchone()[0]
    code_after = conn.execute("SELECT COUNT(*) c FROM environments WHERE code=?", (CODE,)).fetchone()[0]
    max_id_after = conn.execute("SELECT MAX(id) m FROM environments").fetchone()[0]
    second_row = conn.execute(
        "SELECT id FROM environments WHERE code=? AND id<>?", (CODE, first_id)
    ).fetchone()
    conn.close()

    # 正式环境册零增：总行数、同编码行数、最大 id 都不得因第二笔变化。
    assert code_after == 1
    assert total_after == total_before, "第二笔不得让正式环境册增行"
    assert max_id_after == max_id_before, "第二笔不得产生新的正式环境行 id"
    assert second_row is None, "查出同编码第二行 = 埋雷未封死"


def test_create_env_writes_official_row_audit_and_history(client, tmp_path):
    """正常不同编码创建：正式落行，审计与变更历史同笔留痕。"""
    db_path = tmp_path / "gbf01_write.db"
    h = _h(client)

    r = client.post(
        "/api/envs",
        headers=h,
        json={"code": "apac-new", "title": "亚太新环境", "risk_level": 1},
    )
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    assert r.json()["code"] == "apac-new"

    conn = sqlite3.connect(db_path)
    env = conn.execute("SELECT code, title, risk_level FROM environments WHERE id=?", (eid,)).fetchone()
    audits = conn.execute(
        "SELECT action FROM audit_log WHERE entity='environment' AND entity_id=?", (eid,)
    ).fetchall()
    histories = conn.execute(
        "SELECT action FROM change_history WHERE env_id=?", (eid,)
    ).fetchall()
    conn.close()

    assert env == ("apac-new", "亚太新环境", 1)
    assert any(a[0] == "create_env" for a in audits)
    assert any(x[0] == "create_env" for x in histories)


def test_viewer_forbidden_to_create_env(client):
    """登录不动，但写口仍须 writer 以上角色。"""
    h = _h(client, "viewer", "viewer123")
    r = client.post("/api/envs", headers=h, json={"code": "x-1", "title": "X"})
    assert r.status_code == 403
