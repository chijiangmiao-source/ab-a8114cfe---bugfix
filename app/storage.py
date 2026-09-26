"""复核记录的 SQLite 持久化。

提交被接受（输入合法）后，无论结论可行还是不可行，都保存一条复核
记录，供刷新后凭复核编号取回。输入非法的请求在接口层直接拒绝，
不产生记录（旧证据由前端清除）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid

DB_PATH = os.environ.get("APP_DB", "/data/locator.db")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                review_id   TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                payload     TEXT NOT NULL,
                conclusion  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submission_keys (
                submission_key TEXT PRIMARY KEY,
                review_id      TEXT NOT NULL,
                FOREIGN KEY (review_id) REFERENCES submissions(review_id)
            )
            """
        )


def save_submission(
    payload: dict,
    conclusion: dict,
    submission_key: str | None = None,
) -> str:
    review_id = uuid.uuid4().hex[:12]
    with _lock, _connect() as conn:
        if isinstance(submission_key, str) and submission_key:
            row = conn.execute(
                "SELECT review_id FROM submission_keys WHERE submission_key = ?",
                (submission_key,),
            ).fetchone()
            if row is not None:
                return row["review_id"]
        # 极小概率撞号时重试一次。
        while True:
            try:
                conn.execute(
                    "INSERT INTO submissions (review_id, payload, conclusion) "
                    "VALUES (?, ?, ?)",
                    (
                        review_id,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(conclusion, ensure_ascii=False),
                    ),
                )
                break
            except sqlite3.IntegrityError:
                review_id = uuid.uuid4().hex[:12]
        if isinstance(submission_key, str) and submission_key:
            conn.execute(
                "INSERT INTO submission_keys (submission_key, review_id) VALUES (?, ?)",
                (submission_key, review_id),
            )
    return review_id


def load_submission_for_key(submission_key: str) -> dict | None:
    if not isinstance(submission_key, str) or not submission_key:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT review_id FROM submission_keys WHERE submission_key = ?",
            (submission_key,),
        ).fetchone()
    if row is None:
        return None
    return load_submission(row["review_id"])


def load_submission(review_id: str) -> dict | None:
    if not review_id or not all(c in "0123456789abcdef" for c in review_id):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT review_id, created_at, payload, conclusion "
            "FROM submissions WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "input": json.loads(row["payload"]),
        "conclusion": json.loads(row["conclusion"]),
    }
