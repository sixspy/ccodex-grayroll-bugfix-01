"""环境目录与环境编码创建（甲路径：同编码第二笔整笔拒写，正式册零增）。"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from rules_svc import env_rule_audit
from validators import require_int, require_str

router = APIRouter(prefix="/api/envs", tags=["envs"])

# 「环境编码创建互斥」：按编码分键的行锁，与 flag-create:{key} 同一家族约定。
# 争用与取舍拆开——acquire 只负责串行化并给败方落 contention_marks；
# 持锁后的编码唯一联查才决定第二笔整笔拒写（甲路径）。
ENV_CREATE_LOCK_PREFIX = "env-create"


def env_create_lock_key(code: str) -> str:
    return f"{ENV_CREATE_LOCK_PREFIX}:{code}"


class EnvIn(BaseModel):
    code: str
    title: str
    risk_level: int = 1


@router.get("")
def list_envs(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM environments ORDER BY risk_level, id").fetchall()]
    return {"items": rows}


@router.get("/consistency")
def env_consistency(authorization: str | None = Header(default=None)):
    """环境编码唯一 × 规则外键联查账（与 /api/rules/completeness 同一本账）。"""
    current_user(authorization)
    with connect() as conn:
        return env_rule_audit(conn)


@router.get("/{env_id}")
def get_env(env_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="环境不存在")
    return dict(row)


@router.post("")
def create_env(body: EnvIn, authorization: str | None = Header(default=None)):
    """甲路径创建口：环境编码创建互斥 + 持锁唯一联查 + DB UNIQUE 兜底。

    同编码第二笔整笔拒写（400），正式环境册零增；锁竞争败方 409 且只落 contention_marks。
    """
    user = current_user(authorization)
    require_writer(user)
    code = require_str(body.code, "环境编码", 2)
    title = require_str(body.title, "环境名称", 1)
    risk_level = require_int(body.risk_level, "风险等级", 0, 10)
    lock_key = env_create_lock_key(code)

    with connect() as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        # 争用核销第一段：抢锁败方仅在 locks 服务内落 contention_marks，随后 409。
        try:
            acquire(conn, lock_key, user["username"])
        except HTTPException as exc:
            if exc.status_code == 409:
                raise HTTPException(
                    status_code=409,
                    detail="环境编码创建互斥：另一会话正在创建该编码，争用败方仅留痕，不写正式册",
                ) from exc
            raise

        new_id: int | None = None
        try:
            # 争用核销第二段（命运取舍）：持锁后做编码唯一联查。
            existed = conn.execute(
                "SELECT id FROM environments WHERE code=?", (code,)
            ).fetchone()
            if existed:
                # 同编码第二笔：整笔拒写，不写 environments / audit_log / change_history。
                raise HTTPException(
                    status_code=400,
                    detail="环境编码已存在，同编码第二笔整笔拒写（正式环境册零增）",
                )
            try:
                cur = conn.execute(
                    "INSERT INTO environments(code, title, risk_level) VALUES(?,?,?)",
                    (code, title, risk_level),
                )
            except sqlite3.IntegrityError as exc:
                # DB UNIQUE 兜底（多会话/多进程竞态）：回滚未竟插入，整笔拒写。
                conn.rollback()
                raise HTTPException(
                    status_code=400,
                    detail="环境编码已存在（唯一约束兜底），同编码第二笔整笔拒写",
                ) from exc
            new_id = cur.lastrowid
            write_audit(conn, user["username"], "create_env", "environment", new_id, code)
            write_history(
                conn,
                user["username"],
                "create_env",
                f"登记环境编码 {code}",
                env_id=new_id,
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, code, title, risk_level FROM environments WHERE id=?", (new_id,)
            ).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user["username"])
            conn.commit()


def _rule_is_newer(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    return (a["version"], a["updated_at"], a["id"]) > (
        b["version"],
        b["updated_at"],
        b["id"],
    )


def _override_is_preferred(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    return (a["active"], a["updated_at"], a["id"]) > (
        b["active"],
        b["updated_at"],
        b["id"],
    )


# 只做 env_id 改挂、不存在 (flag_id,env_id,...) 唯一碰撞的子表。
_REPOINT_TABLES = (
    "rule_shadows",
    "releases",
    "release_shadows",
    "kill_events",
    "kill_shadows",
    "rollout_schedules",
    "change_history",
)


def reconcile_env_codes(conn) -> dict:
    """同事务跨表把同码双行并轨为 canonical 单行，并把规则等外键全部改挂单行。

    账实口径：每组同码环境保留 MIN(id) 作为 canonical；flag_rules / overrides
    因有复合 UNIQUE，碰撞时按确定性胜负合并去重，其余子表整批改挂，最后删双行。
    本函数不自行 commit，由调用方保证整笔一个事务。
    """
    groups = conn.execute(
        "SELECT code, GROUP_CONCAT(id) AS ids, MIN(id) AS canonical_id, COUNT(*) AS c "
        "FROM environments GROUP BY code HAVING c > 1 ORDER BY code"
    ).fetchall()

    report: dict = {
        "reconciled_codes": 0,
        "groups": [],
        "repointed": {},
        "merged_rules": 0,
        "merged_overrides": 0,
        "removed_env_ids": [],
    }

    for g in groups:
        code = g["code"]
        canonical = int(g["canonical_id"])
        ids = sorted(int(x) for x in str(g["ids"]).split(","))
        extras = [i for i in ids if i != canonical]
        if not extras:
            continue

        # 1) flag_rules：先解决 (flag_id, env_id, official) 唯一碰撞，再改挂。
        for extra_id in extras:
            extra_rules = conn.execute(
                "SELECT * FROM flag_rules WHERE env_id=? ORDER BY id", (extra_id,)
            ).fetchall()
            for rx in extra_rules:
                clash = conn.execute(
                    "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=?",
                    (rx["flag_id"], canonical, rx["official"]),
                ).fetchone()
                if clash is None:
                    conn.execute(
                        "UPDATE flag_rules SET env_id=? WHERE id=?", (canonical, rx["id"])
                    )
                elif _rule_is_newer(rx, clash):
                    conn.execute("DELETE FROM flag_rules WHERE id=?", (clash["id"],))
                    conn.execute(
                        "UPDATE flag_rules SET env_id=? WHERE id=?", (canonical, rx["id"])
                    )
                    report["merged_rules"] += 1
                else:
                    conn.execute("DELETE FROM flag_rules WHERE id=?", (rx["id"],))
                    report["merged_rules"] += 1

        # 2) overrides：解决 (flag_id, env_id, subject_key) 唯一碰撞。
        for extra_id in extras:
            extra_ovs = conn.execute(
                "SELECT * FROM overrides WHERE env_id=? ORDER BY id", (extra_id,)
            ).fetchall()
            for ov in extra_ovs:
                clash = conn.execute(
                    "SELECT * FROM overrides WHERE flag_id=? AND env_id=? AND subject_key=?",
                    (ov["flag_id"], canonical, ov["subject_key"]),
                ).fetchone()
                if clash is None:
                    conn.execute(
                        "UPDATE overrides SET env_id=? WHERE id=?", (canonical, ov["id"])
                    )
                elif _override_is_preferred(ov, clash):
                    conn.execute("DELETE FROM overrides WHERE id=?", (clash["id"],))
                    conn.execute(
                        "UPDATE overrides SET env_id=? WHERE id=?", (canonical, ov["id"])
                    )
                    report["merged_overrides"] += 1
                else:
                    conn.execute("DELETE FROM overrides WHERE id=?", (ov["id"],))
                    report["merged_overrides"] += 1

        # 3) 无复合唯一碰撞的子表整批改挂。
        placeholders = ",".join("?" for _ in extras)
        for table in _REPOINT_TABLES:
            cur = conn.execute(
                f"UPDATE {table} SET env_id=? WHERE env_id IN ({placeholders})",  # noqa: S608
                [canonical, *extras],
            )
            if cur.rowcount:
                report["repointed"][table] = report["repointed"].get(table, 0) + cur.rowcount

        # 4) 所有外键已改挂，删除同码多余环境行。
        conn.execute(
            f"DELETE FROM environments WHERE id IN ({placeholders})",  # noqa: S608
            extras,
        )

        report["groups"].append(
            {"code": code, "canonical_id": canonical, "merged_env_ids": extras}
        )
        report["removed_env_ids"].extend(extras)
        report["reconciled_codes"] += 1

    return report


@router.post("/reconcile")
def post_reconcile(authorization: str | None = Header(default=None)):
    """联查并轨：同事务跨表清理同码双行，使规则只挂单行。"""
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        report = reconcile_env_codes(conn)
        if report["reconciled_codes"]:
            write_audit(
                conn,
                user["username"],
                "env_code_reconcile",
                "environment",
                None,
                json.dumps(report, ensure_ascii=False)[:480],
            )
            write_history(
                conn,
                user["username"],
                "env_code_reconcile",
                f"环境编码同事务并轨 {report['reconciled_codes']} 组，规则改挂单行",
            )
        conn.commit()
    return report
