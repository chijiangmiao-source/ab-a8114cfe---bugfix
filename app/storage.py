"""复核记录的 SQLite 持久化。

提交被接受（输入合法）后，无论结论可行还是不可行，都保存一条复核
记录，供刷新后凭复核编号取回。输入非法的请求在接口层直接拒绝，
不产生记录（旧证据由前端清除）。

幂等提交身份（submission_key）
------------------------------
客户端可为一次逻辑提交附带 submission_key：

* 相同 submission_key + 相同规范化内容 → 返回首次保存的同一条记录
  （网络重试安全，稳定取得同一复核编号与证据）；
* 相同 submission_key + 不同内容 → 抛出 SubmissionConflictError，
  接口层以 409 明确拒绝，绝不回放旧结果；
* 绑定关系与内容摘要持久化在同一 SQLite 库中，服务重启后重试语义、
  既有复核编号与各自证据保持一致。

并发相同重试由进程内锁 + submission_key 主键约束共同保证只形成
一条记录。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid

DB_PATH = os.environ.get("APP_DB", "/data/locator.db")

_lock = threading.Lock()


class SubmissionConflictError(RuntimeError):
    """同一 submission_key 被用于与已存记录不同的内容。"""

    def __init__(self, review_id: str):
        self.review_id = review_id
        super().__init__(f"submission_key 已绑定复核记录 {review_id}")


def content_hash(payload: dict) -> str:
    """规范化提交内容的稳定摘要（与 JSON 键序无关）。

    对规范化载荷（排序后的通道 + 规范化校验列表）取摘要，使同一
    逻辑提交的重试得到相同摘要；任何观测内容差异都会改变摘要。
    """
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
                content_hash   TEXT,
                FOREIGN KEY (review_id) REFERENCES submissions(review_id)
            )
            """
        )
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """为既有数据库补充 content_hash 列并按已存载荷回填摘要。"""
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(submission_keys)").fetchall()
    }
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE submission_keys ADD COLUMN content_hash TEXT")
    rows = conn.execute(
        "SELECT sk.submission_key AS k, s.payload AS p "
        "FROM submission_keys sk JOIN submissions s ON s.review_id = sk.review_id "
        "WHERE sk.content_hash IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE submission_keys SET content_hash = ? WHERE submission_key = ?",
            (content_hash(json.loads(row["p"])), row["k"]),
        )


def save_submission(
    payload: dict,
    conclusion: dict,
    submission_key: str | None = None,
) -> tuple[str, bool]:
    """保存复核记录，返回 (复核编号, 是否新建)。

    submission_key 已绑定时：内容摘要一致 → 返回原记录（created=False，
    网络重试幂等）；不一致 → 抛 SubmissionConflictError（409 由接口层
    返回）。绑定检查与插入在同一临界区完成，两个相同重试并发到达也
    只会形成一条记录。
    """
    key = submission_key if isinstance(submission_key, str) and submission_key else None
    digest = content_hash(payload)
    with _lock, _connect() as conn:
        if key is not None:
            row = conn.execute(
                "SELECT review_id, content_hash FROM submission_keys "
                "WHERE submission_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if row["content_hash"] == digest:
                    return row["review_id"], False
                raise SubmissionConflictError(row["review_id"])
        review_id = _insert_submission(conn, payload, conclusion)
        if key is not None:
            conn.execute(
                "INSERT INTO submission_keys (submission_key, review_id, content_hash) "
                "VALUES (?, ?, ?)",
                (key, review_id, digest),
            )
        return review_id, True


def _insert_submission(conn: sqlite3.Connection, payload: dict, conclusion: dict) -> str:
    review_id = uuid.uuid4().hex[:12]
    # 极小概率撞号时重试。
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
            return review_id
        except sqlite3.IntegrityError:
            review_id = uuid.uuid4().hex[:12]


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
