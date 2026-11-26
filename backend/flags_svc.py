"""功能开关登记。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from models import FLAG_STATUS_LABEL
from validators import require_flag_key, require_str

router = APIRouter(prefix="/api/flags", tags=["flags"])


class FlagIn(BaseModel):
    key: str
    title: str
    description: str = ""
    owner: str = "writer"


@router.get("")
def list_flags(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM flags ORDER BY id").fetchall()]
    for r in rows:
        r["status_label"] = FLAG_STATUS_LABEL.get(r["status"], r["status"])
    return {"items": rows}


@router.get("/{flag_id}")
def get_flag(flag_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM flags WHERE id=?", (flag_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="开关不存在")
    d = dict(row)
    d["status_label"] = FLAG_STATUS_LABEL.get(d["status"], d["status"])
    return d


@router.post("")
def create_flag(body: FlagIn, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    key = require_flag_key(body.key)
    title = require_str(body.title, "标题", 2)
    desc = (body.description or "").strip()
    owner = require_str(body.owner or user["username"], "负责人", 2)
    # grayroll-bugfix-01 甲路径（当班封死，禁止乙路径影子草案折中）：
    # 同 key 只许一行正式开关。开关键创建锁串行化并发创建，撞键第二笔正式零增拒写。
    lock_key = f"flag-create:{key}"
    with connect() as conn:
        # 争用核销第一段：抢锁败方仅落 contention_marks 争用痕（在 locks 服务内），409 返回
        acquire(conn, lock_key, user["username"], detail="开关键创建锁争用，请稍后重试")
        try:
            # 争用核销第二段（命运取舍）：持锁后做 key 唯一校验
            existed = conn.execute("SELECT id FROM flags WHERE key=?", (key,)).fetchone()
            if existed:
                # 撞键第二笔：正式零增拒写，不写 flags / audit_log / change_history
                raise HTTPException(400, detail="开关键已存在，撞键第二笔拒写")
            now = time.time()
            cur = conn.execute(
                "INSERT INTO flags(key, title, description, owner, status, created_at) VALUES(?,?,?,?,?,?)",
                (key, title, desc, owner, "draft", now),
            )
            fid = cur.lastrowid
            write_audit(conn, user["username"], "create_flag", "flag", fid, key)
            write_history(conn, user["username"], "create_flag", f"登记开关 {key}", flag_id=fid)
            conn.commit()
        finally:
            release(conn, lock_key, user["username"])
            conn.commit()
    return {"id": fid, "key": key, "status": "draft"}


@router.post("/{flag_id}/archive")
def archive_flag(flag_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        row = conn.execute("SELECT * FROM flags WHERE id=?", (flag_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail="开关不存在")
        if row["status"] == "archived":
            raise HTTPException(400, detail="已归档")
        conn.execute("UPDATE flags SET status='archived' WHERE id=?", (flag_id,))
        write_history(conn, user["username"], "archive", f"归档 {row['key']}", flag_id=flag_id)
        write_audit(conn, user["username"], "archive", "flag", flag_id, row["key"])
        conn.commit()
    return {"id": flag_id, "status": "archived"}
