"""接口冒烟测试：提交、复核取回、非法输入拒绝、不可行结论持久化、健康检查，
以及提交身份的幂等语义（独立保存 / 同次重试一致 / 异内容重用拒绝 /
并发重试单记录 / 非法内容不占用身份也不回放旧证据）。

以 Python 标准库 urllib 起真实 HTTP 线程，无需第三方依赖。
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

# 让测试可以独立运行；且必须在导入应用模块前设置 DB 路径
# （storage 在导入时读取 APP_DB）。
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import server  # noqa: E402
import storage  # noqa: E402

# 两组合法但结论不同的观测（同一通道集合、不同校验内容）。
OBSERVATION_A = {
    "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
    "checks": [
        {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
        {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
        {"channels": ["CH3", "CH5"], "parity": 1},
        {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
    ],
}
# 仅翻转最后一条校验的观测奇偶：最优解变为 CH1/CH4/CH5。
OBSERVATION_B = {
    "channels": OBSERVATION_A["channels"],
    "checks": [dict(c) for c in OBSERVATION_A["checks"][:-1]]
    + [{"channels": ["CH0", "CH2", "CH4"], "parity": 1}],
}


def fresh_key() -> str:
    return f"test-{uuid.uuid4().hex}"


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
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

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
        body = {
            "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
            "checks": [
                {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
                {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
                {"channels": ["CH3", "CH5"], "parity": 1},
                {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
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

    # ---- 提交身份（submission_key）幂等语义 ----

    def test_two_observations_on_same_page_saved_independently(self):
        # 同页连续两次不同观测（不同提交身份）：各自独立保存。
        body_a = dict(OBSERVATION_A, submission_key=fresh_key())
        status, data_a = self._req("POST", "/api/submit", body_a)
        self.assertEqual(status, 200, data_a)
        rid_a = data_a["review_id"]
        self.assertEqual(data_a["conclusion"]["faulty"], ["CH3"])

        body_b = dict(OBSERVATION_B, submission_key=fresh_key())
        status, data_b = self._req("POST", "/api/submit", body_b)
        self.assertEqual(status, 200, data_b)
        rid_b = data_b["review_id"]
        self.assertEqual(data_b["conclusion"]["faulty"], ["CH1", "CH4", "CH5"])

        self.assertNotEqual(rid_a, rid_b)
        # 两条记录各自取回，证据互不串扰。
        _, got_a = self._req("GET", f"/api/review/{rid_a}")
        _, got_b = self._req("GET", f"/api/review/{rid_b}")
        self.assertEqual(got_a["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(got_b["conclusion"]["faulty"], ["CH1", "CH4", "CH5"])
        self.assertEqual(got_a["input"]["checks"][-1]["parity"], 0)
        self.assertEqual(got_b["input"]["checks"][-1]["parity"], 1)

    def test_retry_same_key_same_content_returns_same_record(self):
        body = dict(OBSERVATION_A, submission_key=fresh_key())
        status1, first = self._req("POST", "/api/submit", body)
        status2, second = self._req("POST", "/api/submit", body)
        self.assertEqual((status1, status2), (200, 200))
        self.assertEqual(first["review_id"], second["review_id"])
        self.assertEqual(first["conclusion"], second["conclusion"])
        self.assertEqual(first["input"], second["input"])

    def test_same_key_different_content_rejected_409(self):
        key = fresh_key()
        status, first = self._req("POST", "/api/submit",
                                  dict(OBSERVATION_A, submission_key=key))
        self.assertEqual(status, 200)
        rid = first["review_id"]

        status, data = self._req("POST", "/api/submit",
                                 dict(OBSERVATION_B, submission_key=key))
        self.assertEqual(status, 409, data)
        self.assertEqual(data["field"], "submission_key")
        self.assertTrue(any(e["field"] == "submission_key"
                            for e in data["errors"]))
        # 既有记录不被覆盖、不回放为新结论。
        _, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(got["input"]["checks"][-1]["parity"], 0)

    def test_concurrent_identical_retries_form_single_record(self):
        key = fresh_key()
        body = dict(OBSERVATION_A, submission_key=key)

        def one_attempt(_):
            return self._req("POST", "/api/submit", body)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one_attempt, range(8)))
        statuses = {s for s, _ in results}
        ids = {d["review_id"] for _, d in results}
        self.assertEqual(statuses, {200}, results)
        self.assertEqual(len(ids), 1, f"并发重试产生了多条记录: {ids}")

        # 存储层同样只有一条身份绑定、一条记录。
        with storage._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM submission_keys WHERE submission_key = ?",
                (key,),
            ).fetchone()
        self.assertEqual(row["c"], 1)

    def test_invalid_content_with_reused_key_returns_400_not_old_evidence(self):
        # 先合法提交取得编号；随后同一身份携带非法内容必须 400，
        # 绝不返回第一次的旧证据。
        key = fresh_key()
        status, first = self._req("POST", "/api/submit",
                                  dict(OBSERVATION_A, submission_key=key))
        self.assertEqual(status, 200)
        rid = first["review_id"]

        bad = {
            "channels": ["CH0", "CH1", "CH0"],          # 重复通道
            "checks": [{"channels": [], "parity": 9}],  # 空集合 + 非法奇偶
            "submission_key": key,
        }
        status, data = self._req("POST", "/api/submit", bad)
        self.assertEqual(status, 400, data)
        self.assertNotIn("review_id", data)
        self.assertNotIn("conclusion", data)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("channels[2]", fields)

        # 旧记录保持原样可取回。
        _, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

    def test_invalid_submission_does_not_claim_key(self):
        # 400 不占用提交身份：修正后以同一身份可正常保存。
        key = fresh_key()
        bad = dict(OBSERVATION_A, submission_key=key)
        bad["checks"] = [{"channels": [], "parity": 0}]
        status, _ = self._req("POST", "/api/submit", bad)
        self.assertEqual(status, 400)

        status, data = self._req("POST", "/api/submit",
                                 dict(OBSERVATION_A, submission_key=key))
        self.assertEqual(status, 200, data)
        self.assertEqual(data["conclusion"]["faulty"], ["CH3"])

    def test_keyless_submissions_always_create_new_records(self):
        status1, first = self._req("POST", "/api/submit", dict(OBSERVATION_A))
        status2, second = self._req("POST", "/api/submit", dict(OBSERVATION_A))
        self.assertEqual((status1, status2), (200, 200))
        self.assertNotEqual(first["review_id"], second["review_id"])

    def test_malformed_submission_key_rejected(self):
        status, data = self._req("POST", "/api/submit",
                                 dict(OBSERVATION_A, submission_key=12345))
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["field"], "submission_key")

        status, data = self._req("POST", "/api/submit",
                                 dict(OBSERVATION_A, submission_key="x" * 200))
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["field"], "submission_key")

    def test_restart_preserves_records_and_retry_semantics(self):
        # 模拟服务重启：全新进程/连接读取同一数据库文件，
        # 既有编号、重试一致性与 409 语义保持不变。
        key = fresh_key()
        body = dict(OBSERVATION_A, submission_key=key)
        status, first = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 200)
        rid = first["review_id"]

        # 重新打开数据库（等价于重启后重新连接同一持久化文件）。
        record = storage.load_submission(rid)
        self.assertIsNotNone(record)
        self.assertEqual(record["conclusion"]["faulty"], ["CH3"])

        # 重启后同身份同内容重试 -> 同一记录；异内容 -> 409。
        outcome, retry_id, _ = storage.save_submission(
            record["input"], record["conclusion"], key)
        self.assertEqual((outcome, retry_id), ("existing", rid))
        other_payload = dict(record["input"])
        other_payload["checks"] = other_payload["checks"][:-1]
        outcome, conflict_id, _ = storage.save_submission(
            other_payload, record["conclusion"], key)
        self.assertEqual((outcome, conflict_id), ("conflict", rid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
