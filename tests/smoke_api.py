"""Compose verify 使用的 API 冒烟脚本：对运行中的 app 服务发真实请求。

覆盖：健康检查、唯一故障定位、多解同重量裁决、不可行结论持久化、
非法输入（可定位拒绝）、复核取回，以及提交身份的幂等语义：

- 同页连续两次不同观测各自独立保存；
- 同一逻辑提交的网络重试稳定返回同一记录；
- 同一提交身份被用于不同内容时以 409 明确拒绝（不回放旧结果）；
- 并发到达的相同重试只形成一条记录；
- 非法内容即使复用既有身份也只得到 400，不会显示旧证据；
- 服务重启（以同一持久化数据库启动全新进程）后，复核取回、
  重试一致性与 409 语义保持不变。

任一步失败以退出码 1 结束。设置 SMOKE_RECHECK_ONLY=1 时进入
“复核取回”模式：不重复提交，仅按状态文件核对（可能已重启的）
目标服务上的既有记录，用于在容器重启后做第二轮验证。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("APP_URL", "http://app:8080").rstrip("/")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_ID = uuid.uuid4().hex[:8]

# 两组合法但结论不同的观测：同一通道集合，第二组翻转了末条校验的奇偶。
OBSERVATION_A = {
    "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
    "checks": [
        {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
        {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
        {"channels": ["CH3", "CH5"], "parity": 1},
        {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
    ],
}
OBSERVATION_B = {
    "channels": OBSERVATION_A["channels"],
    "checks": [dict(c) for c in OBSERVATION_A["checks"][:-1]]
    + [{"channels": ["CH0", "CH2", "CH4"], "parity": 1}],
}
EXPECTED_A = ["CH3"]
EXPECTED_B = ["CH1", "CH4", "CH5"]


def call(method, path, body=None, base=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request((base or BASE) + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"冒烟失败: {name} {detail}")


def keyed(body, tag):
    return dict(body, submission_key=f"smoke-{RUN_ID}-{tag}")


# ----------------------------------------------------------------------
# 复核取回核对：既可用于重启后的全新进程，也可用于外部重启后的服务
# ----------------------------------------------------------------------
def recheck_records(base, state, label):
    """核对既有复核编号、重试一致性与异内容拒绝（不对服务写入新内容）。"""
    print(f"复核取回核对（{label}: {base}）")

    for tag in ("unique", "second", "infeasible"):
        entry = state[tag]
        status, got = call("GET", f"/api/review/{entry['review_id']}", base=base)
        check(f"[{label}] 记录 {tag} 可取回", status == 200,
              f"status={status}")
        if tag == "infeasible":
            check(f"[{label}] 记录 {tag} 仍为不可行结论",
                  got["conclusion"]["feasible"] is False
                  and got["conclusion"]["faulty"] == [])
        else:
            check(f"[{label}] 记录 {tag} 故障通道一致",
                  got["conclusion"]["faulty"] == entry["expected_faulty"],
                  str(got["conclusion"].get("faulty")))
            check(f"[{label}] 记录 {tag} 输入与提交一致",
                  got["input"]["channels"] == entry["body"]["channels"]
                  and [c["parity"] for c in got["input"]["checks"]]
                  == [c["parity"] for c in entry["body"]["checks"]])

    # 重启后重试同一逻辑提交：必须稳定返回同一复核编号。
    entry = state["unique"]
    retry_body = dict(entry["body"], submission_key=entry["key"])
    status, again = call("POST", "/api/submit", retry_body, base=base)
    check(f"[{label}] 重试返回同一复核编号",
          status == 200 and again.get("review_id") == entry["review_id"],
          f"status={status} got={again.get('review_id')}")

    # 重启后同一身份携带不同内容：必须 409 拒绝。
    other = dict(entry["body"])
    other["checks"] = other["checks"][:-1]  # 去掉一条校验 -> 不同内容
    other["submission_key"] = entry["key"]
    status, data = call("POST", "/api/submit", other, base=base)
    check(f"[{label}] 异内容重用身份被 409 拒绝",
          status == 409 and data.get("field") == "submission_key",
          f"status={status} body={data}")


# ----------------------------------------------------------------------
# 主功能检查（对运行中的 app 服务）
# ----------------------------------------------------------------------
def run_functional_checks():
    print(f"API 冒烟目标: {BASE}")
    state = {}

    # 0. 健康检查
    status, body = call("GET", "/healthz")
    check("健康检查 200", status == 200 and body.get("status") == "ok")

    # 1. 第一组观测（唯一故障：仅 CH3）
    key_a = f"smoke-{RUN_ID}-a"
    body_a = dict(OBSERVATION_A, submission_key=key_a)
    status, data = call("POST", "/api/submit", body_a)
    check("观测一提交 200", status == 200, str(data))
    check("观测一故障 = CH3 且重量 1",
          data["conclusion"]["faulty"] == EXPECTED_A
          and data["conclusion"]["weight"] == 1,
          str(data["conclusion"].get("faulty")))
    check("观测一逐校验复算全部一致",
          all(r["pass"] for r in data["conclusion"]["recompute"]))
    rid_a = data["review_id"]
    state["unique"] = {
        "key": key_a, "body": OBSERVATION_A,
        "review_id": rid_a, "expected_faulty": EXPECTED_A,
    }

    # 2. 同页第二次提交：保留通道、修改校验内容（新的提交身份），
    #    必须独立保存为新记录，且第一条记录不受影响。
    key_b = f"smoke-{RUN_ID}-b"
    body_b = dict(OBSERVATION_B, submission_key=key_b)
    status, data = call("POST", "/api/submit", body_b)
    check("观测二提交 200", status == 200, str(data))
    rid_b = data["review_id"]
    check("观测二独立保存（新复核编号）", rid_b != rid_a, rid_b)
    check("观测二故障 = CH1/CH4/CH5",
          data["conclusion"]["faulty"] == EXPECTED_B,
          str(data["conclusion"].get("faulty")))
    status, got = call("GET", f"/api/review/{rid_a}")
    check("观测一记录未被第二次提交污染",
          status == 200 and got["conclusion"]["faulty"] == EXPECTED_A
          and got["input"]["checks"][-1]["parity"] == 0)
    status, got = call("GET", f"/api/review/{rid_b}")
    check("观测二记录可取回且内容对应本次观测",
          status == 200 and got["conclusion"]["faulty"] == EXPECTED_B
          and got["input"]["checks"][-1]["parity"] == 1)
    state["second"] = {
        "key": key_b, "body": OBSERVATION_B,
        "review_id": rid_b, "expected_faulty": EXPECTED_B,
    }

    # 3. 同一逻辑提交的网络重试：同一身份 + 同一内容 -> 同一记录。
    status, retry1 = call("POST", "/api/submit", body_a)
    status2, retry2 = call("POST", "/api/submit", body_a)
    check("重试一致（同一复核编号）",
          status == 200 and status2 == 200
          and retry1["review_id"] == rid_a
          and retry2["review_id"] == rid_a,
          f"{retry1.get('review_id')} {retry2.get('review_id')} != {rid_a}")
    check("重试返回的结论与首次一致",
          retry1["conclusion"]["faulty"] == EXPECTED_A)

    # 4. 同一身份被用于不同内容：必须 409 明确拒绝，不回放旧结果。
    conflict_body = dict(OBSERVATION_B, submission_key=key_a)
    status, data = call("POST", "/api/submit", conflict_body)
    check("异内容重用身份返回 409", status == 409, f"status={status}")
    check("409 错误可定位到 submission_key",
          data.get("field") == "submission_key"
          and any(e.get("field") == "submission_key"
                  for e in data.get("errors", [])),
          str(data))
    status, got = call("GET", f"/api/review/{rid_a}")
    check("409 后既有记录保持原证据",
          status == 200 and got["conclusion"]["faulty"] == EXPECTED_A)

    # 5. 并发到达的相同重试：全部成功且只形成一条记录。
    key_c = f"smoke-{RUN_ID}-c"
    body_c = dict(OBSERVATION_A, submission_key=key_c)

    def one_attempt(_):
        return call("POST", "/api/submit", body_c)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one_attempt, range(8)))
    check("并发重试全部 200", all(s == 200 for s, _ in results),
          str([s for s, _ in results]))
    ids = {d["review_id"] for _, d in results}
    check("并发重试只形成一个复核编号", len(ids) == 1, str(ids))

    # 6. 不可行：结论必须持久化而非返回近似集合。
    key_inf = f"smoke-{RUN_ID}-inf"
    body_inf = {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["a", "b", "c"], "parity": 0},
            {"channels": ["c"], "parity": 1},
        ],
        "submission_key": key_inf,
    }
    status, data = call("POST", "/api/submit", body_inf)
    check("不可行提交 200（保存不可行结论）", status == 200, str(data))
    check("结论为不可行且无故障集合",
          data["conclusion"]["feasible"] is False
          and data["conclusion"]["faulty"] == [],
          str(data.get("conclusion")))
    rid_inf = data["review_id"]
    status, got = call("GET", f"/api/review/{rid_inf}")
    check("不可行记录可取回且仍不可行",
          status == 200 and got["conclusion"]["feasible"] is False)
    state["infeasible"] = {
        "key": key_inf,
        "body": {k: v for k, v in body_inf.items() if k != "submission_key"},
        "review_id": rid_inf,
    }

    # 7. 多解裁决：{a,b,c} 奇偶 1 有三个重量 1 解，字典序裁决给 c。
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
    })
    check("多解提交 200", status == 200, str(data))
    check("多解同重量裁决给 c", data["conclusion"]["faulty"] == ["c"],
          str(data["conclusion"].get("faulty")))

    # 8. 非法输入：可定位拒绝（重复通道 / 空集合 / 非法奇偶）。
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "a"],
        "checks": [{"channels": [], "parity": 9}],
    })
    check("非法输入返回 400", status == 400, str(status))
    fields = {e["field"] for e in data.get("errors", [])}
    check("错误信息可定位（channels[2]）", "channels[2]" in fields, str(fields))
    check("错误信息可定位（空集合）",
          any(f.endswith(".channels") for f in fields), str(fields))
    check("错误信息可定位（parity）",
          any(f.endswith(".parity") for f in fields), str(fields))

    # 9. 非法内容即使复用既有身份，也只得到 400，绝不返回旧证据。
    bad_reused = {
        "channels": ["CH0", "CH1", "CH0"],
        "checks": [{"channels": [], "parity": 9}],
        "submission_key": key_a,
    }
    status, data = call("POST", "/api/submit", bad_reused)
    check("复用身份的非法内容返回 400 而非旧证据",
          status == 400 and "review_id" not in data
          and "conclusion" not in data,
          f"status={status} body={data}")
    status, got = call("GET", f"/api/review/{rid_a}")
    check("旧记录证据未被非法请求污染",
          status == 200 and got["conclusion"]["faulty"] == EXPECTED_A)

    # 10. 400 不占用提交身份：修正后以同一身份可正常保存。
    key_fix = f"smoke-{RUN_ID}-fix"
    bad = dict(OBSERVATION_A, submission_key=key_fix)
    bad["checks"] = [{"channels": [], "parity": 0}]
    status, _ = call("POST", "/api/submit", bad)
    check("非法提交返回 400", status == 400)
    status, data = call("POST", "/api/submit",
                        dict(OBSERVATION_A, submission_key=key_fix))
    check("修正后以同一身份可保存",
          status == 200 and data["conclusion"]["faulty"] == EXPECTED_A,
          f"status={status}")

    # 11. 重复校验集合被拒绝。
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["b", "a"], "parity": 1},
        ],
    })
    check("重复校验集合返回 400", status == 400)
    check("重复原因可读", any("重复" in e["message"] for e in data.get("errors", [])))

    # 12. 无身份的相同内容两次提交：各自独立保存。
    status, first = call("POST", "/api/submit", dict(OBSERVATION_A))
    status2, second = call("POST", "/api/submit", dict(OBSERVATION_A))
    check("无身份提交各自独立保存",
          status == 200 and status2 == 200
          and first["review_id"] != second["review_id"])

    return state


# ----------------------------------------------------------------------
# 重启持久性：以同一数据库文件启动全新服务进程再核对
# ----------------------------------------------------------------------
def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def restart_persistence_check(state):
    db = os.environ.get("RESTART_CHECK_DB")
    if not db:
        default = "/data/locator.db"
        if os.path.exists(default):
            db = default
        else:
            print("  [SKIP] 重启持久性检查：未找到共享数据库，"
                  "本地运行时可设 RESTART_CHECK_DB 指向 app 的 DB 文件")
            return
    check("重启检查：共享数据库文件存在", os.path.exists(db), db)

    port = _free_port()
    env = dict(os.environ)
    env.update({"APP_DB": db, "APP_PORT": str(port), "APP_HOST": "127.0.0.1"})
    proc = subprocess.Popen(
        [sys.executable, os.path.join("app", "server.py")],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15
        up = False
        while time.time() < deadline:
            try:
                status, body = call("GET", "/healthz", base=base)
                if status == 200 and body.get("status") == "ok":
                    up = True
                    break
            except urllib.error.URLError:
                time.sleep(0.2)
        check("重启后的服务进程已就绪", up)
        # 全新进程 + 同一持久化数据库 == 服务重启：
        # 既有复核编号、重试一致性与 409 语义必须保持不变。
        recheck_records(base, state, label="重启后实例")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ----------------------------------------------------------------------
# 状态文件：供外部重启（docker compose restart app）后的第二轮核对
# ----------------------------------------------------------------------
def _state_path():
    path = os.environ.get("SMOKE_STATE_FILE")
    if path:
        return path
    return "/data/smoke_state.json" if os.path.isdir("/data") else None


def save_state(state):
    path = _state_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        print(f"复核状态已写入 {path}（供重启后第二轮核对）")
    except OSError as exc:
        print(f"  [WARN] 状态文件写入失败（不影响本轮结果）: {exc}")


def load_state():
    path = _state_path()
    if not path or not os.path.exists(path):
        raise SystemExit(f"未找到复核状态文件（SMOKE_STATE_FILE={path}），"
                         "请先运行完整冒烟再进入复核取回模式")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    if os.environ.get("SMOKE_RECHECK_ONLY") == "1":
        recheck_records(BASE, load_state(), label="目标服务")
        print("重启后复核取回核对全部通过")
        return
    state = run_functional_checks()
    save_state(state)
    restart_persistence_check(state)
    print("API 冒烟全部通过")


if __name__ == "__main__":
    main()
