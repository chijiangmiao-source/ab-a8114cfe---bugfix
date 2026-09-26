"""接口测试：提交、复核取回、非法输入拒绝、不可行结论持久化、健康检查，
以及提交身份的幂等/冲突/并发/重启语义。

以 Python 标准库 urllib 起真实 HTTP 线程，无需第三方依赖。
"""

import concurrent.futures
import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 让测试可以独立运行；且必须在导入应用模块前设置 DB 路径
# （storage 在导入时读取 APP_DB）。
import sys

_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import server  # noqa: E402
import storage  # noqa: E402


def http_json(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


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


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, body=None):
        return http_json(self.base, method, path, body)

    def test_healthz(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn("故障定位", resp.read().decode("utf-8"))

    def test_submit_unique_fault_and_review(self):
        status, data = self._req("POST", "/api/submit", copy.deepcopy(OBSERVATION_A))
        self.assertEqual(status, 200, data)
        rid = data["review_id"]
        self.assertTrue(rid)
        self.assertEqual(data["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(data["conclusion"]["weight"], 1)
        self.assertTrue(all(r["pass"] for r in data["conclusion"]["recompute"]))

        # 刷新后凭编号取回
        status2, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status2, 200)
        self.assertEqual(got["review_id"], rid)
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

    def test_infeasible_is_persisted(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["a", "b", "c"], "parity": 0},
                {"channels": ["c"], "parity": 1},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 200)
        self.assertFalse(data["conclusion"]["feasible"])
        rid = data["review_id"]
        status2, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status2, 200)
        self.assertFalse(got["conclusion"]["feasible"])
        self.assertEqual(got["conclusion"]["faulty"], [])

    def test_invalid_input_rejected_with_location(self):
        # 重复通道 + 空校验集合 + 非法奇偶
        body = {
            "channels": ["a", "b", "a"],
            "checks": [{"channels": [], "parity": 9}],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("channels[2]", fields)
        self.assertTrue(any(".channels" in f for f in fields))
        self.assertTrue(any(".parity" in f for f in fields))

    def test_duplicate_check_set_rejected(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["b", "a"], "parity": 1},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        self.assertTrue(any("重复" in e["message"] for e in data["errors"]))

    def test_bad_json_rejected(self):
        req = urllib.request.Request(
            self.base + "/api/submit",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data["field"], "body")

    def test_unknown_review_id_404(self):
        status, data = self._req("GET", "/api/review/deadbeefdead")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")

    # ---- 提交身份（submission_key）语义 ----

    def test_same_page_two_observations_saved_independently(self):
        """同页连续两次不同观测：各自生成独立记录，证据互不串扰。"""
        body_a = copy.deepcopy(OBSERVATION_A)
        body_a["submission_key"] = "page-key-obs-a"
        status_a, data_a = self._req("POST", "/api/submit", body_a)
        self.assertEqual(status_a, 200, data_a)

        body_b = observation_b()
        body_b["submission_key"] = "page-key-obs-b"  # 新的逻辑提交 -> 新身份
        status_b, data_b = self._req("POST", "/api/submit", body_b)
        self.assertEqual(status_b, 200, data_b)

        self.assertNotEqual(data_a["review_id"], data_b["review_id"])
        # 各自取回：输入与结论只对应本次观测
        _, got_a = self._req("GET", f"/api/review/{data_a['review_id']}")
        _, got_b = self._req("GET", f"/api/review/{data_b['review_id']}")
        self.assertEqual(got_a["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(got_b["conclusion"]["faulty"], ["CH1", "CH4", "CH5"])
        self.assertEqual(got_a["input"]["checks"][3]["parity"], 0)
        self.assertEqual(got_b["input"]["checks"][3]["parity"], 1)
        self.assertTrue(all(r["pass"] for r in got_b["conclusion"]["recompute"]))

    def test_retry_same_key_same_content_returns_same_record(self):
        body = copy.deepcopy(OBSERVATION_A)
        body["submission_key"] = "retry-key-1"
        s1, d1 = self._req("POST", "/api/submit", body)
        s2, d2 = self._req("POST", "/api/submit", body)
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(d1["review_id"], d2["review_id"])
        self.assertEqual(d1["input"], d2["input"])
        self.assertEqual(d1["conclusion"], d2["conclusion"])

    def test_same_key_different_content_rejected_409(self):
        key = "conflict-key-1"
        body_a = copy.deepcopy(OBSERVATION_A)
        body_a["submission_key"] = key
        s1, d1 = self._req("POST", "/api/submit", body_a)
        self.assertEqual(s1, 200, d1)

        body_b = observation_b()
        body_b["submission_key"] = key  # 同一身份，不同内容
        s2, d2 = self._req("POST", "/api/submit", body_b)
        self.assertEqual(s2, 409, d2)
        self.assertTrue(any(e["field"] == "submission_key" for e in d2["errors"]))
        self.assertEqual(d2.get("existing_review_id"), d1["review_id"])

        # 旧记录不被覆盖、也不回放新内容
        _, got = self._req("GET", f"/api/review/{d1['review_id']}")
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(got["input"]["checks"][3]["parity"], 0)

    def test_invalid_content_with_used_key_not_replayed(self):
        key = "invalid-after-valid-key"
        body_a = copy.deepcopy(OBSERVATION_A)
        body_a["submission_key"] = key
        s1, d1 = self._req("POST", "/api/submit", body_a)
        self.assertEqual(s1, 200, d1)

        # 同一身份 + 非法内容：必须 400，绝不能回放旧证据
        bad = {"channels": ["a", "b", "a"],
               "checks": [{"channels": [], "parity": 9}],
               "submission_key": key}
        s2, d2 = self._req("POST", "/api/submit", bad)
        self.assertEqual(s2, 400, d2)
        self.assertNotIn("conclusion", d2)
        self.assertIn("errors", d2)

    def test_invalid_attempt_does_not_bind_key(self):
        key = "invalid-first-key"
        bad = {"channels": ["a", "b", "a"],
               "checks": [{"channels": [], "parity": 9}],
               "submission_key": key}
        s1, _ = self._req("POST", "/api/submit", bad)
        self.assertEqual(s1, 400)
        # 非法提交不建档也不绑定身份；修正内容后同身份可正常提交
        good = copy.deepcopy(OBSERVATION_A)
        good["submission_key"] = key
        s2, d2 = self._req("POST", "/api/submit", good)
        self.assertEqual(s2, 200, d2)
        self.assertEqual(d2["conclusion"]["faulty"], ["CH3"])

    def test_concurrent_identical_retries_form_single_record(self):
        body = copy.deepcopy(OBSERVATION_A)
        body["submission_key"] = "concurrent-key-1"

        def post():
            return http_json(self.base, "POST", "/api/submit", body)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: post(), range(8)))
        statuses = {s for s, _ in results}
        ids = {d["review_id"] for _, d in results}
        self.assertEqual(statuses, {200}, results)
        self.assertEqual(len(ids), 1, f"并发重试形成了多条记录: {ids}")

    def test_malformed_submission_key_rejected(self):
        body = copy.deepcopy(OBSERVATION_A)
        body["submission_key"] = 12345
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["field"], "submission_key")


class RestartPersistenceTests(unittest.TestCase):
    """服务重启后：既有复核编号、各自证据与重试语义保持一致。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls._tmp.name, "restart.db")
        server.init_db()
        cls._boot()

    @classmethod
    def _boot(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        storage.DB_PATH = cls._old_db
        cls._tmp.cleanup()

    def _req(self, method, path, body=None):
        return http_json(self.base, method, path, body)

    def test_restart_preserves_review_and_retry_semantics(self):
        body = copy.deepcopy(OBSERVATION_A)
        body["submission_key"] = "restart-key-1"
        s1, d1 = self._req("POST", "/api/submit", body)
        self.assertEqual(s1, 200, d1)
        rid = d1["review_id"]

        # 模拟服务重启：关闭监听，在同一 DB 文件上重新初始化并启动。
        self.httpd.shutdown()
        self.httpd.server_close()
        server.init_db()  # 初始化/迁移必须幂等
        self._boot()

        # 既有复核编号仍可取回，证据逐字段一致
        s, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(s, 200, got)
        self.assertEqual(got["input"], d1["input"])
        self.assertEqual(got["conclusion"], d1["conclusion"])
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

        # 重试语义不变：同身份同内容 -> 同一记录
        s2, d2 = self._req("POST", "/api/submit", body)
        self.assertEqual(s2, 200, d2)
        self.assertEqual(d2["review_id"], rid)

        # 同身份不同内容 -> 仍然 409 拒绝
        other = observation_b()
        other["submission_key"] = "restart-key-1"
        s3, d3 = self._req("POST", "/api/submit", other)
        self.assertEqual(s3, 409, d3)
        self.assertEqual(d3.get("existing_review_id"), rid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
