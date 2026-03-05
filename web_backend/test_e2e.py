from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = PROJECT_ROOT / "web_backend" / "server.py"
TEST_HOST = "127.0.0.1"
TEST_PORT = int(os.getenv("WEB_E2E_TEST_PORT", "8788"))
BASE_URL = f"http://{TEST_HOST}:{TEST_PORT}"


def _get_json(url: str, timeout: float = 5.0) -> tuple[int, dict]:
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body)


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"ok": False, "error": body}
        return exc.code, parsed


class WebBackendE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.proc = subprocess.Popen(
            [sys.executable, str(SERVER_PATH), "--host", TEST_HOST, "--port", str(TEST_PORT)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        deadline = time.time() + 20
        last_error = ""
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                stderr = cls.proc.stderr.read() if cls.proc.stderr else ""
                raise RuntimeError(f"web backend exited early: {stderr}")
            try:
                status, _ = _get_json(f"{BASE_URL}/api/health", timeout=1.5)
                if status == 200:
                    return
            except (URLError, ConnectionError, TimeoutError) as exc:
                last_error = str(exc)
                time.sleep(0.3)

        cls.tearDownClass()
        raise RuntimeError(f"web backend did not become healthy in time: {last_error}")

    @classmethod
    def tearDownClass(cls) -> None:
        proc = getattr(cls, "proc", None)
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def test_health_endpoint(self) -> None:
        status, payload = _get_json(f"{BASE_URL}/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        self.assertIn("web", payload)

    def test_qa_validation_error(self) -> None:
        status, payload = _post_json(f"{BASE_URL}/api/qa", {"question": ""})
        self.assertEqual(status, 400)
        self.assertFalse(payload.get("ok", True))
        self.assertIn("question", payload.get("error", ""))

    def test_qa_smoke(self) -> None:
        _, health = _get_json(f"{BASE_URL}/api/health")
        if not health.get("index_exists"):
            self.skipTest("index not built; skip QA smoke")

        status, payload = _post_json(
            f"{BASE_URL}/api/qa",
            {
                "question": "VCS two-step flow 和 three-step flow 有什么区别？",
                "guide": "vcs",
                "top_k": 5,
                "max_pages_for_llm": 4,
                "language": "zh-CN",
            },
            timeout=60.0,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        self.assertIn("answer_final", payload)
        self.assertIn("citations", payload)
        self.assertIn("llm", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
