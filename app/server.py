"""噪声故障定位 Web 接口（仅依赖 Python 标准库）。

路由
----
GET  /                     录入页面
GET  /healthz              健康检查
POST /api/submit           提交通道与校验，求解并保存结论，返回复核编号
GET  /api/review/<id>      按复核编号取回提交内容与结论

提交语义
--------
每次合法提交（含不可行结论）都独立保存一条复核记录。客户端为每次
逻辑提交生成 ``submission_key``：同一身份 + 相同内容的网络重试返回
同一条记录；同一身份被用于不同内容时以 409 明确拒绝，绝不回放旧
结果；非法输入以 400 拒绝且不占用提交身份、不返回任何旧证据。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from solver import ValidationError, recompute, solve
from storage import (
    init_db,
    load_submission,
    save_submission,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 1 << 20  # 1 MiB
MAX_KEY_LEN = 128


class Handler(BaseHTTPRequestHandler):
    server_version = "PixelLocator/1.0"

    # ---- 工具 ----
    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    # ---- GET ----
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/" or route == "/index.html":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif route == "/static/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
        elif route == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif route.startswith("/api/review/"):
            review_id = route.rsplit("/", 1)[-1]
            record = load_submission(review_id)
            if record is None:
                self._send_json(404, {
                    "error": "复核编号不存在",
                    "field": "review_id",
                    "review_id": review_id,
                })
            else:
                self._send_json(200, record)
        else:
            self._send_json(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route != "/api/submit":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {
                "error": "请求体缺失或过大",
                "field": "body",
                "errors": [{"field": "body", "message": "需要 JSON 请求体且不超过 1MiB"}],
            })
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {
                "error": "JSON 解析失败",
                "field": "body",
                "errors": [{"field": "body", "message": f"JSON 解析失败: {exc}"}],
            })
            return
        if not isinstance(data, dict):
            self._send_json(400, {
                "error": "请求体必须是对象",
                "field": "body",
                "errors": [{"field": "body", "message": "请求体必须是 JSON 对象"}],
            })
            return

        submission_key = data.get("submission_key")
        if submission_key is not None:
            if not isinstance(submission_key, str):
                self._send_json(400, {
                    "error": "提交身份非法",
                    "field": "submission_key",
                    "errors": [{"field": "submission_key",
                                "message": "提交身份必须是字符串"}],
                })
                return
            submission_key = submission_key.strip()
            if not submission_key:
                submission_key = None
            elif len(submission_key) > MAX_KEY_LEN:
                self._send_json(400, {
                    "error": "提交身份非法",
                    "field": "submission_key",
                    "errors": [{"field": "submission_key",
                                "message": f"提交身份长度不得超过 {MAX_KEY_LEN} 字符"}],
                })
                return

        channels = data.get("channels")
        checks = data.get("checks")
        # 先校验输入：非法内容一律 400 拒绝，不触碰提交身份绑定，
        # 也绝不因此返回任何既有记录（旧证据由前端清除）。
        try:
            result = solve(channels, checks)
        except ValidationError as exc:
            self._send_json(400, {
                "error": "输入校验未通过，未生成复核记录",
                "errors": [{"field": f, "message": msg} for f, msg in exc.errors],
            })
            return

        # 用规范化（排序后）的通道与原始提交的校验做逐校验复算。
        # solve 内部对通道排序、对校验去重；为保持复算条目与用户
        # 提交顺序一致，这里重新规范化一次校验集合。
        ordered = list(result.channels)
        norm_checks = _normalize_checks_for_recompute(ordered, checks)
        recomputed = recompute(ordered, norm_checks, list(result.vector)) if result.feasible else []

        conclusion = {
            "feasible": result.feasible,
            "weight": result.weight,
            "faulty": list(result.faulty),
            "vector": (
                {ch: bit for ch, bit in zip(result.channels, result.vector)}
                if result.feasible else {}
            ),
            "left_size": result.left_size,
            "left_index_size": result.left_index_size,
            "recompute": recomputed,
            "message": (
                f"最小故障通道 {result.weight} 个：{', '.join(result.faulty)}"
                if result.feasible else "不存在能同时满足全部异或约束的故障向量（不可行）"
            ),
        }
        payload = {"channels": ordered, "checks": [
            {"channels": list(members), "parity": parity}
            for members, parity in norm_checks
        ]}
        outcome, review_id, stored = save_submission(payload, conclusion, submission_key)
        if outcome == "conflict":
            # 同一提交身份被用于不同内容：明确拒绝，不保存、不回放。
            self._send_json(409, {
                "error": "提交身份已绑定其它观测内容，本次提交被拒绝",
                "field": "submission_key",
                "errors": [{
                    "field": "submission_key",
                    "message": (
                        "该提交身份已用于不同的观测内容"
                        f"（已绑定复核编号 {review_id}）；"
                        "这是一次新的观测时，请以新的提交身份重新提交"
                    ),
                }],
                "existing_review_id": review_id,
            })
            return
        if outcome == "existing":
            # 同一逻辑提交的网络重试：稳定返回既有记录。
            self._send_json(200, stored)
            return
        self._send_json(200, {
            "review_id": review_id,
            "input": payload,
            "conclusion": conclusion,
        })


def _normalize_checks_for_recompute(ordered_channels, raw_checks):
    """把提交的校验按已排序通道名规范化（去本条重复、去整组重复）。

    与求解器一致，同时接受对象形式 {"channels": [...], "parity": 0/1}
    与 [通道集合, 奇偶值] 数组形式。
    """
    seen_sets: set[frozenset[str]] = set()
    out = []
    for ck in raw_checks:
        if isinstance(ck, dict):
            raw_members = ck.get("channels", [])
            raw_parity = ck.get("parity")
        else:
            raw_members, raw_parity = ck[0], ck[1]
        members = []
        local: set[str] = set()
        for name in raw_members:
            name = name.strip()
            if name in ordered_channels and name not in local:
                local.add(name)
                members.append(name)
        members.sort()
        key = frozenset(members)
        if members and key not in seen_sets:
            seen_sets.add(key)
            out.append((tuple(members), int(raw_parity)))
    return out


def main() -> None:
    init_db()
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"pixel locator listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
