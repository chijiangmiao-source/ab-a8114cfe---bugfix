"""重启持久化冒烟：真实 HTTP 验证服务重启后的复核取回与重试语义。

在同一数据库文件上两次启动 app 服务进程（模拟服务重启）：

1. 第一次启动：提交带 submission_key 的观测，取得复核编号；
2. 进程终止后第二次启动（同一 DB 文件）：
   - 凭复核编号取回，输入/最小故障向量/逐校验复算逐字段一致；
   - 同身份同内容重试 -> 返回同一复核编号（不新建记录）；
   - 同身份不同内容 -> 409 明确拒绝，不回放旧结果。

任一步失败以退出码 1 结束。Compose verify 服务在本容器内运行本脚本，
使用独立临时数据库，不干扰 app 服务的数据卷。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "app", "server.py")

DB_PATH = os.environ.get("APP_RESTART_DB", "/tmp/restart-smoke.db")
PORT = int(os.environ.get("APP_RESTART_PORT", "18099"))
BASE = f"http://127.0.0.1:{PORT}"

OBSERVATION = {
    "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
    "checks": [
        {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
        {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
        {"channels": ["CH3", "CH5"], "parity": 1},
        {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
    ],
    "submission_key": "restart-smoke-key",
}


def call(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"重启冒烟失败: {name} {detail}")


def start_server():
    env = dict(os.environ)
    env.update({"APP_DB": DB_PATH, "APP_PORT": str(PORT), "APP_HOST": "127.0.0.1"})
    proc = subprocess.Popen(
        [sys.executable, SERVER], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"服务进程过早退出，退出码 {proc.returncode}")
        try:
            status, body = call("GET", "/healthz")
            if status == 200 and body.get("status") == "ok":
                return proc
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("服务在 15s 内未就绪")


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    print(f"重启冒烟目标: {BASE}（DB: {DB_PATH}）")

    # ---- 第一次启动：提交并取得复核编号 ----
    proc = start_server()
    try:
        status, data = call("POST", "/api/submit", OBSERVATION)
        check("首次提交 200", status == 200, str(data))
        rid = data["review_id"]
        check("唯一故障 = CH3", data["conclusion"]["faulty"] == ["CH3"],
              str(data["conclusion"].get("faulty")))
        saved_input = data["input"]
        saved_conclusion = data["conclusion"]
    finally:
        stop_server(proc)

    # ---- 第二次启动（同一 DB）：复核取回与重试语义须一致 ----
    proc = start_server()
    try:
        status, got = call("GET", f"/api/review/{rid}")
        check("重启后复核编号可取回", status == 200, str(status))
        check("重启后输入一致", got["input"] == saved_input)
        check("重启后结论一致（故障向量/逐校验复算）",
              got["conclusion"] == saved_conclusion)

        status, retry = call("POST", "/api/submit", OBSERVATION)
        check("重启后同身份同内容重试 200", status == 200, str(retry))
        check("重试返回同一复核编号（未新建记录）",
              retry["review_id"] == rid,
              f"{retry.get('review_id')} != {rid}")

        other = json.loads(json.dumps(OBSERVATION))
        other["checks"][3]["parity"] = 1  # 同身份，不同内容
        status, conflict = call("POST", "/api/submit", other)
        check("重启后同身份不同内容 409 拒绝", status == 409, str(status))
        check("409 指向 submission_key 且引用既有记录",
              any(e.get("field") == "submission_key"
                  for e in conflict.get("errors", []))
              and conflict.get("existing_review_id") == rid,
              str(conflict))

        status, got = call("GET", f"/api/review/{rid}")
        check("冲突后旧记录证据未被篡改",
              status == 200 and got["conclusion"] == saved_conclusion)
    finally:
        stop_server(proc)
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)

    print("重启持久化冒烟全部通过")


if __name__ == "__main__":
    main()
