"""Compose verify 使用的 API 冒烟脚本：对运行中的 app 服务发真实请求。

覆盖：健康检查、唯一故障定位、多解同重量裁决、不可行结论持久化、
非法输入（可定位拒绝）、复核取回，以及提交身份语义——同页两次不同
观测各自建档、同次重试幂等、异内容重用拒绝、并发重试只建一条记录。
任一步失败以退出码 1 结束。
"""

import concurrent.futures
import copy
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("APP_URL", "http://app:8080").rstrip("/")

# 每轮冒烟使用全新提交身份，重复执行（同一数据卷）也不受历史绑定影响。
RUN = uuid.uuid4().hex[:10]


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
        raise SystemExit(f"冒烟失败: {name} {detail}")


# 观测一：唯一故障解，仅 CH3 失效。
OBSERVATION_A = {
    "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
    "checks": [
        {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
        {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
        {"channels": ["CH3", "CH5"], "parity": 1},
        {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
    ],
}


def observation_b():
    """观测二：保留通道集合，修改一条校验的观测奇偶 -> 另一组故障通道。"""
    body = copy.deepcopy(OBSERVATION_A)
    body["checks"][3]["parity"] = 1
    return body


def main():
    print(f"API 冒烟目标: {BASE}")

    # 0. 健康检查
    status, body = call("GET", "/healthz")
    check("健康检查 200", status == 200 and body.get("status") == "ok")

    # 1. 唯一故障：仅 CH3
    status, data = call("POST", "/api/submit", copy.deepcopy(OBSERVATION_A))
    check("唯一故障提交 200", status == 200, str(data))
    check("唯一故障 = CH3 且重量 1",
          data["conclusion"]["faulty"] == ["CH3"]
          and data["conclusion"]["weight"] == 1,
          str(data["conclusion"].get("faulty")))
    check("逐校验复算全部一致",
          all(r["pass"] for r in data["conclusion"]["recompute"]))
    rid_unique = data["review_id"]

    # 2. 多解裁决：{a,b,c} 奇偶 1 有三个重量 1 解，
    #    选择向量标准字典序裁决给 c（(0,0,1) 最小）。
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
    })
    check("多解提交 200", status == 200, str(data))
    check("多解同重量裁决给 c", data["conclusion"]["faulty"] == ["c"],
          str(data["conclusion"].get("faulty")))

    # 3. 不可行：结论必须持久化而非返回近似集合
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["a", "b", "c"], "parity": 0},
            {"channels": ["c"], "parity": 1},
        ],
    })
    check("不可行提交 200（保存不可行结论）", status == 200, str(data))
    check("结论为不可行且无故障集合",
          data["conclusion"]["feasible"] is False
          and data["conclusion"]["faulty"] == [],
          str(data.get("conclusion")))
    rid_infeasible = data["review_id"]

    # 4. 刷新后取回两条记录
    status, got = call("GET", f"/api/review/{rid_unique}")
    check("唯一故障记录可取回", status == 200
          and got["conclusion"]["faulty"] == ["CH3"])
    status, got = call("GET", f"/api/review/{rid_infeasible}")
    check("不可行记录可取回且仍不可行",
          status == 200 and got["conclusion"]["feasible"] is False)

    # 5. 非法输入：可定位拒绝（重复通道 / 空集合 / 非法奇偶）
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

    # 6. 重复校验集合被拒绝
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["b", "a"], "parity": 1},
        ],
    })
    check("重复校验集合返回 400", status == 400)
    check("重复原因可读", any("重复" in e["message"] for e in data.get("errors", [])))

    # 7. 同页连续两次不同观测：各自保存独立复核记录
    key_a, key_b = f"page-a-{RUN}", f"page-b-{RUN}"
    body_a = copy.deepcopy(OBSERVATION_A)
    body_a["submission_key"] = key_a
    status, first = call("POST", "/api/submit", body_a)
    check("第一次观测提交 200", status == 200, str(first))

    body_b = observation_b()
    body_b["submission_key"] = key_b  # 页面不刷新，但新逻辑提交使用新身份
    status, second = call("POST", "/api/submit", body_b)
    check("第二次观测提交 200", status == 200, str(second))

    check("两次观测复核编号不同",
          first["review_id"] != second["review_id"],
          f"两次返回同一编号 {first['review_id']}")
    status, got_a = call("GET", f"/api/review/{first['review_id']}")
    check("第一次记录证据属于第一次观测",
          status == 200
          and got_a["conclusion"]["faulty"] == ["CH3"]
          and got_a["input"]["checks"][3]["parity"] == 0,
          str(got_a.get("conclusion", {}).get("faulty")))
    status, got_b = call("GET", f"/api/review/{second['review_id']}")
    check("第二次记录证据属于第二次观测（CH1/CH4/CH5）",
          status == 200
          and got_b["conclusion"]["faulty"] == ["CH1", "CH4", "CH5"]
          and got_b["input"]["checks"][3]["parity"] == 1
          and all(r["pass"] for r in got_b["conclusion"]["recompute"]),
          str(got_b.get("conclusion", {}).get("faulty")))

    # 8. 同次重试一致：同身份同内容 -> 同一记录
    retry_key = f"retry-{RUN}"
    retry_body = copy.deepcopy(OBSERVATION_A)
    retry_body["submission_key"] = retry_key
    s1, d1 = call("POST", "/api/submit", retry_body)
    s2, d2 = call("POST", "/api/submit", retry_body)
    check("重试两次均 200", (s1, s2) == (200, 200), f"{s1}/{s2}")
    check("重试返回同一复核编号与证据",
          d1["review_id"] == d2["review_id"]
          and d1["input"] == d2["input"]
          and d1["conclusion"] == d2["conclusion"],
          f"{d1.get('review_id')} vs {d2.get('review_id')}")

    # 9. 异内容重用同一身份：必须 409 拒绝，不回放旧结果
    other = observation_b()
    other["submission_key"] = retry_key
    status, conflict = call("POST", "/api/submit", other)
    check("异内容重用身份返回 409", status == 409, str(status))
    check("409 错误可定位到 submission_key",
          any(e.get("field") == "submission_key"
              for e in conflict.get("errors", [])),
          str(conflict))
    check("409 不携带旧结论（不回放）", "conclusion" not in conflict)
    status, got = call("GET", f"/api/review/{d1['review_id']}")
    check("冲突后原记录证据不变",
          status == 200 and got["conclusion"] == d1["conclusion"])

    # 10. 非法内容 + 已用身份：必须 400，不得回放旧证据
    bad = {"channels": ["a", "b", "a"],
           "checks": [{"channels": [], "parity": 9}],
           "submission_key": retry_key}
    status, rejected = call("POST", "/api/submit", bad)
    check("非法内容配已用身份返回 400", status == 400, str(status))
    check("400 不携带旧结论", "conclusion" not in rejected)
    check("400 错误可定位", "channels[2]" in
          {e.get("field") for e in rejected.get("errors", [])}, str(rejected))

    # 11. 并发相同重试：只能形成一条记录
    conc_key = f"conc-{RUN}"
    conc_body = copy.deepcopy(OBSERVATION_A)
    conc_body["submission_key"] = conc_key

    def post_once(_):
        return call("POST", "/api/submit", conc_body)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(post_once, range(8)))
    check("并发重试全部 200", all(s == 200 for s, _ in results),
          str([s for s, _ in results]))
    ids = {d["review_id"] for _, d in results}
    check("并发重试只形成一个复核编号", len(ids) == 1, str(ids))
    status, again = call("POST", "/api/submit", conc_body)
    check("并发后再重试仍返回同一记录",
          status == 200 and again["review_id"] == ids.pop())

    print("API 冒烟全部通过")


if __name__ == "__main__":
    main()
