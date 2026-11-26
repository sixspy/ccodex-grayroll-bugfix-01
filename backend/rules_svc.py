"""灰度规则只读与影子绑定（乙路径草案）。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from policies import assert_flag_publishable, assert_not_killed_or_allow_kill, describe_path
from validators import require_int, require_percent, require_rule_kind

router = APIRouter(prefix="/api/rules", tags=["rules"])


class ShadowBindIn(BaseModel):
    flag_id: int
    env_id: int
    kind: str
    percent: int = 0
    cohort_id: int | None = None


def env_rule_audit(conn) -> dict:
    """环境编码唯一性 × 规则外键挂接的联查账。

    甲口径下正式环境册一个编码只能有一行；若历史脏库出现同码双行，
    不能按 code 折叠后把分裂挂接误判成『齐套/绿』。本函数给出：
    - consistent：账实是否相符（无同码双行、无孤儿规则）
    - complete：每个未归档开关 × 每个环境编码是否恰有一条 canonical 正式规则
    - green：consistent 且 complete 且无歧义挂接（任何双行都不许报绿）
    """
    env_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, code, title, risk_level FROM environments ORDER BY id"
        ).fetchall()
    ]
    ids_by_code: dict[str, list[int]] = {}
    for e in env_rows:
        ids_by_code.setdefault(e["code"], []).append(e["id"])

    duplicate_codes = []
    canonical_by_code: dict[str, int] = {}
    for code, ids in ids_by_code.items():
        ordered = sorted(ids)
        canonical_by_code[code] = ordered[0]
        if len(ordered) > 1:
            duplicate_codes.append(
                {
                    "code": code,
                    "env_ids": ordered,
                    "canonical_id": ordered[0],
                    "extra_ids": ordered[1:],
                }
            )

    rule_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT r.id, r.flag_id, r.env_id, r.official, r.version,
                   f.key AS flag_key, f.status AS flag_status, e.code AS env_code
            FROM flag_rules r
            JOIN flags f ON f.id = r.flag_id
            LEFT JOIN environments e ON e.id = r.env_id
            ORDER BY r.id
            """
        ).fetchall()
    ]
    live_env_ids = {e["id"] for e in env_rows}
    orphan_rule_ids = [r["id"] for r in rule_rows if r["env_id"] not in live_env_ids]

    flags = [
        dict(r)
        for r in conn.execute("SELECT id, key, status FROM flags ORDER BY id").fetchall()
    ]
    codes = sorted(ids_by_code.keys())
    coverage = []
    all_covered = True
    any_ambiguous = False
    for f in flags:
        if f["status"] == "archived":
            continue
        for code in codes:
            code_env_ids = set(ids_by_code[code])
            canonical = canonical_by_code[code]
            official_cell = [
                r
                for r in rule_rows
                if r["flag_id"] == f["id"]
                and r["official"] == 1
                and r["env_id"] in code_env_ids
            ]
            canonical_rules = [r for r in official_cell if r["env_id"] == canonical]
            extra_rules = [r for r in official_cell if r["env_id"] != canonical]
            multi_canonical = len(canonical_rules) > 1
            # 双行编码下只要规则落在多行上即为挂接歧义；canonical 多条同样歧义。
            ambiguous = multi_canonical or (
                len(ids_by_code[code]) > 1 and len(official_cell) > 0
            )
            covered = len(canonical_rules) == 1
            if not covered:
                all_covered = False
            if ambiguous:
                any_ambiguous = True
            coverage.append(
                {
                    "flag_id": f["id"],
                    "flag_key": f["key"],
                    "env_code": code,
                    "canonical_env_id": canonical,
                    "rule_count": len(official_cell),
                    "canonical_rule_count": len(canonical_rules),
                    "misattached_rule_count": len(extra_rules),
                    "covered": covered,
                    "ambiguous": ambiguous,
                }
            )

    consistent = not duplicate_codes and not orphan_rule_ids
    complete = bool(coverage) and all_covered
    green = consistent and complete and not any_ambiguous
    collisions = [
        {
            "code": d["code"],
            "env_code": d["code"],
            "c": len(d["env_ids"]),
            "env_ids": d["env_ids"],
            "canonical_id": d["canonical_id"],
        }
        for d in duplicate_codes
    ]
    return {
        "consistent": consistent,
        "complete": complete,
        "green": green,
        "ok": consistent,
        "env_total": len(env_rows),
        "env_code_count": len(codes),
        "duplicate_codes": duplicate_codes,
        "collisions": collisions,
        "orphan_rule_ids": orphan_rule_ids,
        "coverage": coverage,
    }


@router.get("/completeness")
@router.get("/coverage")
@router.get("/audit")
def rule_completeness(authorization: str | None = Header(default=None)):
    """环境编码唯一 × 规则外键挂接齐套核对（双行环境不得假绿）。"""
    current_user(authorization)
    with connect() as conn:
        return env_rule_audit(conn)


@router.get("")
def list_rules(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        official = [
            dict(r)
            for r in conn.execute(
                """
                SELECT r.*, f.key AS flag_key, e.code AS env_code
                FROM flag_rules r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                WHERE r.official=1
                ORDER BY r.id
                """
            ).fetchall()
        ]
        shadows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM rule_shadows ORDER BY id DESC"
            ).fetchall()
        ]
        audit = env_rule_audit(conn)
    return {
        "official": official,
        "shadows": shadows,
        "audit": audit,
        "collisions": audit["collisions"],
    }


@router.post("/shadow-bind")
def shadow_bind(body: ShadowBindIn, authorization: str | None = Header(default=None)):
    """乙路径：只写影子人群/比例绑定，正式规则零改。"""
    user = current_user(authorization)
    require_writer(user)
    fid = require_int(body.flag_id, "开关", 1)
    eid = require_int(body.env_id, "环境", 1)
    kind = require_rule_kind(body.kind)
    pct = require_percent(body.percent)
    key = f"flag-rule:{fid}:{eid}"
    with connect() as conn:
        acquire(conn, key, user["username"])
        try:
            fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
            if not fl:
                raise HTTPException(404, detail="开关不存在")
            assert_flag_publishable(fl["status"])
            assert_not_killed_or_allow_kill(fl["status"])
            if not conn.execute("SELECT 1 FROM environments WHERE id=?", (eid,)).fetchone():
                raise HTTPException(404, detail="环境不存在")
            if body.cohort_id and not conn.execute(
                "SELECT 1 FROM cohorts WHERE id=?", (body.cohort_id,)
            ).fetchone():
                raise HTTPException(404, detail="人群不存在")
            now = time.time()
            cur = conn.execute(
                "INSERT INTO rule_shadows(flag_id, env_id, kind, percent, cohort_id, status, created_at, created_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (fid, eid, kind, pct, body.cohort_id, "open", now, user["username"]),
            )
            sid = cur.lastrowid
            write_audit(conn, user["username"], "rule_shadow", "rule_shadow", sid, describe_path("cohort_bind", True))
            write_history(conn, user["username"], "rule_shadow", f"影子绑定 flag={fid} env={eid}", flag_id=fid, env_id=eid)
            conn.commit()
            return {"mode": "shadow", "id": sid, "path": describe_path("cohort_bind", True)}
        finally:
            release(conn, key, user["username"])
            conn.commit()


@router.post("/shadow/{shadow_id}/promote")
def promote_rule_shadow(shadow_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        sh = conn.execute("SELECT * FROM rule_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh or sh["status"] != "open":
            raise HTTPException(400, detail="影子绑定不可升正")
        fid, eid = sh["flag_id"], sh["env_id"]
        key = f"flag-rule:{fid}:{eid}"
        acquire(conn, key, user["username"])
        try:
            now = time.time()
            old = conn.execute(
                "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
            ).fetchone()
            ver = (old["version"] + 1) if old else 1
            if old:
                conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
            cur = conn.execute(
                "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                "VALUES(?,?,?,?,?,?,1,?)",
                (fid, eid, sh["kind"], sh["percent"], sh["cohort_id"], ver, now),
            )
            rid = cur.lastrowid
            conn.execute("UPDATE rule_shadows SET status='promoted' WHERE id=?", (shadow_id,))
            if conn.execute("SELECT status FROM flags WHERE id=?", (fid,)).fetchone()["status"] == "draft":
                conn.execute("UPDATE flags SET status='active' WHERE id=?", (fid,))
            write_history(conn, user["username"], "rule_promote", f"影子规则升正 v{ver}", flag_id=fid, env_id=eid)
            write_audit(conn, user["username"], "rule_promote", "flag_rule", rid, f"from_shadow={shadow_id}")
            conn.commit()
            return {"id": rid, "version": ver, "mode": "official"}
        finally:
            release(conn, key, user["username"])
            conn.commit()
