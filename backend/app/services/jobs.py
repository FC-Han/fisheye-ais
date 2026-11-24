"""异步分析任务队列（SQLite 实现，不引入 Redis）。

为什么需要它
------------
有些分析不是 1–5 秒能算完的：给一条轨迹取海洋环境要访问 Copernicus 远程服务，
一次瓦片请求 10 秒左右；区域级多船分析更慢。这类任务如果同步跑，
会把 HTTP 连接和 WebSocket 一起挂住。

设计
----
- 表已经在 `analysis_jobs` / `analysis_results`（见 core/db.py），这里只做读写封装
- 生产者只负责 `enqueue()` 拿到 job_id 立刻返回
- 消费者是独立进程 `analytics/job_worker.py`，用 `claim_next()` 抢占任务
- 抢占用 `UPDATE ... WHERE status='queued'` 加 rowcount 判断，SQLite 下足够安全
- 同参数任务自动去重：避免用户连点三次就发三次远程请求
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import closing

from core.db import connect

# 已注册的任务类型。worker 按 kind 分发，未知 kind 直接标失败，不会静默丢任务。
KINDS = {
    "ocean_track": "单船轨迹环境特征提取",
    "behavior_batch": "多船行为时间线批量重算",
}

ACTIVE = ("queued", "running")


def _fingerprint(kind: str, params: dict) -> str:
    raw = json.dumps({"kind": kind, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def enqueue(kind: str, params: dict, dedupe: bool = True) -> dict:
    """提交任务，返回 {"job_id", "deduped"}。

    dedupe=True 时，若同 kind+params 的任务还在排队/运行，直接复用那个 job_id。
    """
    if kind not in KINDS:
        raise ValueError(f"未知任务类型: {kind}")

    now = int(time.time())
    fp = _fingerprint(kind, params)
    job_id = f"{kind}-{fp}"

    with closing(connect()) as conn:
        if dedupe:
            row = conn.execute(
                "SELECT job_id, status FROM analysis_jobs "
                "WHERE job_id = ? AND status IN ('queued','running')",
                (job_id,),
            ).fetchone()
            if row:
                return {"job_id": row["job_id"], "status": row["status"], "deduped": True}

        # 同一个 job_id 之前跑完过：覆盖成新的排队任务，保留历史结果不删
        conn.execute(
            """INSERT INTO analysis_jobs (job_id, kind, params, status, progress, created_at)
               VALUES (?,?,?,'queued',0,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 params=excluded.params, status='queued', progress=0,
                 result_ref=NULL, error=NULL, created_at=excluded.created_at,
                 started_at=NULL, finished_at=NULL""",
            (job_id, kind, json.dumps(params, ensure_ascii=False), now),
        )
        conn.commit()
    return {"job_id": job_id, "status": "queued", "deduped": False}


def claim_next() -> dict | None:
    """worker 用：原子地把一个 queued 任务标成 running 并返回。"""
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT job_id, kind, params FROM analysis_jobs "
            "WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE analysis_jobs SET status='running', started_at=?, progress=0 "
            "WHERE job_id=? AND status='queued'",
            (int(time.time()), row["job_id"]),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None  # 被别的 worker 抢走了
        return {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "params": json.loads(row["params"] or "{}"),
        }


def set_progress(job_id: str, progress: float) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE analysis_jobs SET progress=? WHERE job_id=?",
            (round(max(0.0, min(1.0, progress)), 3), job_id),
        )
        conn.commit()


def finish(job_id: str, result: dict | None = None, error: str | None = None,
           meta: dict | None = None) -> str | None:
    """写入结果快照并结束任务。返回 result_id（失败时 None）。"""
    now = int(time.time())
    result_id = None
    with closing(connect()) as conn:
        if result is not None:
            result_id = f"res-{uuid.uuid4().hex[:12]}"
            meta = meta or {}
            conn.execute(
                """INSERT INTO analysis_results
                   (result_id, job_id, kind, payload, data_version, model_version,
                    parameters, quality_flags, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    result_id,
                    job_id,
                    meta.get("kind"),
                    json.dumps(result, ensure_ascii=False, default=str),
                    meta.get("data_version"),
                    meta.get("model_version"),
                    json.dumps(meta.get("parameters"), ensure_ascii=False, default=str),
                    json.dumps(meta.get("quality_flags"), ensure_ascii=False, default=str),
                    now,
                ),
            )
        conn.execute(
            "UPDATE analysis_jobs SET status=?, progress=?, result_ref=?, error=?, finished_at=? "
            "WHERE job_id=?",
            ("failed" if error else "done", 1.0 if not error else 0.0,
             result_id, error, now, job_id),
        )
        conn.commit()
    return result_id


def get(job_id: str, with_result: bool = False) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        out = {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "kind_label": KINDS.get(row["kind"], row["kind"]),
            "status": row["status"],
            "progress": row["progress"],
            "error": row["error"],
            "params": json.loads(row["params"] or "{}"),
            "result_ref": row["result_ref"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "elapsed_s": (
                (row["finished_at"] or int(time.time())) - row["started_at"]
                if row["started_at"] else None
            ),
        }
        if with_result and row["result_ref"]:
            r = conn.execute(
                "SELECT payload, data_version, model_version, quality_flags, created_at "
                "FROM analysis_results WHERE result_id=?",
                (row["result_ref"],),
            ).fetchone()
            if r:
                out["result"] = json.loads(r["payload"] or "{}")
                out["result_meta"] = {
                    "data_version": r["data_version"],
                    "model_version": r["model_version"],
                    "quality_flags": json.loads(r["quality_flags"] or "null"),
                    "created_at": r["created_at"],
                }
        return out


def list_jobs(limit: int = 20, status: str | None = None) -> list[dict]:
    sql = ("SELECT job_id, kind, status, progress, created_at, started_at, finished_at, error "
           "FROM analysis_jobs")
    args: tuple = ()
    if status:
        sql += " WHERE status=?"
        args = (status,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, (*args, limit))]


def recover_stale(timeout_s: int = 1800) -> int:
    """把卡在 running 太久的任务放回队列（进程被杀时会发生）。"""
    cutoff = int(time.time()) - timeout_s
    with closing(connect()) as conn:
        cur = conn.execute(
            "UPDATE analysis_jobs SET status='queued', progress=0 "
            "WHERE status='running' AND started_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount


def queue_depth() -> dict:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM analysis_jobs GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}
