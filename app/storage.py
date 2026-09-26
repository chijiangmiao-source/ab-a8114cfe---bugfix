"""复核记录的 SQLite 持久化。

提交被接受（输入合法）后，无论结论可行还是不可行，都保存一条复核
记录，供刷新后凭复核编号取回。输入非法的请求在接口层直接拒绝，
不产生记录（旧证据由前端清除），也不占用其提交身份。

提交身份（submission_key）的幂等语义
------------------------------------
客户端为每一次逻辑提交生成一个提交身份；网络重试会携带同一身份、
同一内容再次到达。保存时在同一事务内原子判定：

- 身份未见过            -> 新建独立记录（"created"）；
- 身份已绑定 *相同* 内容 -> 不新建记录，返回既有记录（"existing"），
  并发到达的相同重试因此只会形成一条记录；
- 身份已绑定 *不同* 内容 -> 明确拒绝（"conflict"），不保存、不回放。

绑定关系与记录一样持久化在 SQLite 中，服务重启后重试语义、既有
复核编号与各自证据保持一致。
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


def _record_from_row(row: sqlite3.Row) -> dict:
    return {
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "input": json.loads(row["payload"]),
        "conclusion": json.loads(row["conclusion"]),
    }


def _load_with_conn(conn: sqlite3.Connection, review_id: str) -> dict | None:
    row = conn.execute(
        "SELECT review_id, created_at, payload, conclusion "
        "FROM submissions WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def save_submission(
    payload: dict,
    conclusion: dict,
    submission_key: str | None = None,
) -> tuple[str, str, dict | None]:
    """保存一次合法提交的复核记录。

    返回 (outcome, review_id, record)：

    - ``("created", 新编号, None)``      新记录已独立保存；
    - ``("existing", 既有编号, 既有记录)`` 同一提交身份 + 相同规范化内容
      （网络重试），未新建记录；
    - ``("conflict", 既有编号, 既有记录)`` 同一提交身份被用于不同内容，
      本次内容未保存，调用方必须明确拒绝而非回放。
    """
    review_id = uuid.uuid4().hex[:12]
    with _lock, _connect() as conn:
        if submission_key:
            row = conn.execute(
                "SELECT review_id FROM submission_keys WHERE submission_key = ?",
                (submission_key,),
            ).fetchone()
            if row is not None:
                bound_id = row["review_id"]
                stored = _load_with_conn(conn, bound_id)
                if stored is not None and stored["input"] == payload:
                    return "existing", bound_id, stored
                return "conflict", bound_id, stored
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
        if submission_key:
            conn.execute(
                "INSERT INTO submission_keys (submission_key, review_id) VALUES (?, ?)",
                (submission_key, review_id),
            )
        return "created", review_id, None


def load_submission(review_id: str) -> dict | None:
    if not review_id or not all(c in "0123456789abcdef" for c in review_id):
        return None
    with _connect() as conn:
        return _load_with_conn(conn, review_id)
