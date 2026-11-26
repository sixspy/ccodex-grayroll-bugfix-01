"""gbf01 失败测乙 — 规则服务不得把『双行同编码环境』当成单编码齐套而报假绿。

场景：脏库里 environments 同码 staging-east 有两行（id 91/92），正式规则外键分别挂接，
按 code 折叠会误以为该编码已齐套（假绿）。正确口径：
- 联查 environments + flag_rules 必须识别同码双行与外键错挂，green/齐套 必须为 False；
- 同事务跨表并轨后，同编码只剩单行，规则外键全部改挂 canonical 单行且同 flag/env 仅一条正式规则。

为构造『双行环境』，本文件使用去掉 code UNIQUE 的遗留 schema 建隔离库（模拟已埋雷的旧库）。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
from app import app

CODE = "staging-east"
ENV_A, ENV_B = 91, 92
FLAG_ID = 701


def _legacy_schema() -> str:
    # 埋雷旧库：环境编码没有 UNIQUE，允许同码双行。
    return db.SCHEMA.replace("code TEXT UNIQUE NOT NULL", "code TEXT NOT NULL")


@pytest.fixture()
def legacy_client(tmp_path, monkeypatch):
    db_path = tmp_path / "gbf01_rule_fake.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(_legacy_schema())
    conn.commit()
    conn.close()
    with TestClient(app) as c:
        yield c, db_path


def _token(c: TestClient) -> dict:
    t = c.post("/api/login", json={"username": "admin", "password": "admin123"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _seed_double_env_with_split_rule(db_path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO environments(id, code, title, risk_level) VALUES(?,?,?,?)",
            (ENV_A, CODE, "东部 A 行", 2),
        )
        conn.execute(
            "INSERT INTO environments(id, code, title, risk_level) VALUES(?,?,?,?)",
            (ENV_B, CODE, "东部 B 行", 2),
        )
        now = 1700000000.0
        conn.execute(
            "INSERT INTO flags(id, key, title, description, owner, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (FLAG_ID, "grayroll.gbf01", "GBF01", "", "admin", "active", now),
        )
        # canonical=min(id)=91 上放一条 v1；撞码行 92 上放一条 v3（外键挂接不确定/分裂）。
        conn.execute(
            "INSERT INTO flag_rules(id, flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
            "VALUES(?,?,?,?,?,NULL,?,1,?)",
            (9001, FLAG_ID, ENV_A, "percent", 10, 1, now),
        )
        conn.execute(
            "INSERT INTO flag_rules(id, flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
            "VALUES(?,?,?,?,?,NULL,?,1,?)",
            (9002, FLAG_ID, ENV_B, "percent", 30, 3, now + 1),
        )
        # 跨表子记录挂在撞码行 ENV_B 上，验证并轨时一起改挂。
        conn.execute(
            "INSERT INTO overrides(id, flag_id, env_id, subject_key, effect, note, active, updated_at) "
            "VALUES(?,?,?,?,?,'',1,?)",
            (9101, FLAG_ID, ENV_B, "u-dup", "force_on", now),
        )
        conn.execute(
            "INSERT INTO change_history(flag_id, env_id, action, detail, actor, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (FLAG_ID, ENV_B, "seed_dirty", "挂在双行 B 上的历史", "admin", now),
        )
        conn.commit()
    finally:
        conn.close()


def test_double_row_env_must_not_report_fake_green(legacy_client):
    c, db_path = legacy_client
    _seed_double_env_with_split_rule(db_path)
    h = _token(c)

    r = c.get("/api/rules/completeness", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    # 联查必须抓到同码双行。
    dup = [d for d in body["duplicate_codes"] if d["code"] == CODE]
    assert dup, "必须联查出 staging-east 同码双行"
    assert dup[0]["env_ids"] == [ENV_A, ENV_B]
    assert dup[0]["canonical_id"] == ENV_A

    # 关键：双行 + 外键分裂时，无论按 code 折叠看起来多『齐套』，都不得报绿/齐套。
    assert body["consistent"] is False
    assert body["green"] is False, "把双行环境当单编码齐套 = 假绿，必须封死"

    # 覆盖矩阵里该 flag×code 必须被标 ambiguous，不能干净地算成已覆盖。
    cell = [
        x
        for x in body["coverage"]
        if x["flag_id"] == FLAG_ID and x["env_code"] == CODE
    ]
    assert cell, "联查覆盖项缺失"
    assert cell[0]["ambiguous"] is True


def test_reconcile_repoints_rules_to_single_row_in_one_transaction(legacy_client):
    c, db_path = legacy_client
    _seed_double_env_with_split_rule(db_path)
    h = _token(c)

    r = c.post("/api/envs/reconcile", headers=h, json={})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["reconciled_codes"] >= 1

    conn = sqlite3.connect(db_path)
    try:
        codes = conn.execute(
            "SELECT id FROM environments WHERE code=? ORDER BY id", (CODE,)
        ).fetchall()
        assert [x[0] for x in codes] == [ENV_A], "并轨后同编码只能保留 canonical 单行"

        rule_envs = conn.execute(
            "SELECT DISTINCT env_id FROM flag_rules WHERE flag_id=?", (FLAG_ID,)
        ).fetchall()
        assert [x[0] for x in rule_envs] == [ENV_A], "规则外键必须只挂单行"

        official = conn.execute(
            "SELECT id, version FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, ENV_A),
        ).fetchall()
        assert len(official) == 1, "同 flag×env 的正式规则必须唯一"
        # 合并保留高版本 v3。
        assert official[0][1] == 3

        ov_env = conn.execute(
            "SELECT env_id FROM overrides WHERE id=9101"
        ).fetchone()[0]
        assert ov_env == ENV_A, "overrides 跨表外键须同事务改挂 canonical"

        hist_env = conn.execute(
            "SELECT env_id FROM change_history WHERE action='seed_dirty'"
        ).fetchone()[0]
        assert hist_env == ENV_A, "change_history.env_id 须同事务改挂 canonical"

        gone = conn.execute("SELECT COUNT(*) c FROM environments WHERE id=?", (ENV_B,)).fetchone()[0]
        assert gone == 0
    finally:
        conn.close()

    # 并轨后再核对：一致性恢复，不再假绿。
    r2 = c.get("/api/rules/completeness", headers=h)
    assert r2.status_code == 200
    b2 = r2.json()
    assert all(d["code"] != CODE for d2 in [b2] for d in d2["duplicate_codes"])
    assert b2["consistent"] is True


def test_reconcile_is_idempotent(legacy_client):
    c, db_path = legacy_client
    _seed_double_env_with_split_rule(db_path)
    h = _token(c)
    assert c.post("/api/envs/reconcile", headers=h, json={}).status_code == 200
    again = c.post("/api/envs/reconcile", headers=h, json={})
    assert again.status_code == 200
    assert again.json()["reconciled_codes"] == 0
