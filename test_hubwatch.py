"""Hermetic tests for hubwatch.py: no network, no sleeping. Run: python3 -m unittest -v test_hubwatch"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import hubwatch as w


def up(status=200):
    return {"ok": True, "status": status, "ms": 5}


def down(status=0, why="URLError"):
    return {"ok": False, "status": status, "ms": 5, "why": why}


class Harness:
    """Drives main() run after run with scripted probe results and a fake sender."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sent: list[str] = []
        self.send_ok = True
        self.now = 1_000_000.0
        self.env = {"WATCH_BRIDGE_URL": "https://b.example/health",
                    "WATCH_COCKPIT_URL": "https://c.example/api/version",
                    "WATCHDOG_STATE": str(Path(self.dir.name) / "state" / "state.json")}

    def run(self, bridge, cockpit=None, **extra):
        cockpit = cockpit or up()
        by_url = {self.env["WATCH_BRIDGE_URL"]: bridge, self.env["WATCH_COCKPIT_URL"]: cockpit}

        def send(text, env):
            if self.send_ok:
                self.sent.append(text)
            return self.send_ok

        rc = w.main(env={**self.env, **extra}, now=self.now,
                    probe_fn=lambda url, healthy: dict(by_url[url]), send_fn=send)
        self.now += 300
        return rc

    def state(self):
        return json.loads(Path(self.env["WATCHDOG_STATE"]).read_text())


class TwoStrikeTests(unittest.TestCase):
    def test_one_failed_run_does_not_page(self):
        h = Harness()
        h.run(down())
        h.run(up())
        self.assertEqual(h.sent, [])

    def test_two_failed_runs_page_once_then_stay_quiet(self):
        h = Harness()
        h.run(down())
        h.run(down(502, "HTTP 502"))
        self.assertEqual(len(h.sent), 1)
        self.assertIn("bridge: HTTP 502", h.sent[0])
        for _ in range(5):
            h.run(down())
        self.assertEqual(len(h.sent), 1)

    def test_recovery_sends_exactly_one_message(self):
        h = Harness()
        h.run(down()); h.run(down()); h.run(down())
        h.run(up()); h.run(up())
        self.assertEqual(len(h.sent), 2)
        self.assertIn("RECOVERED", h.sent[1])
        self.assertIn("bridge is answering again", h.sent[1])

    def test_failed_alert_send_retries_next_run(self):
        h = Harness()
        h.run(down())
        h.send_ok = False
        h.run(down())
        self.assertEqual(h.sent, [])
        h.send_ok = True
        h.run(down())
        self.assertEqual(len(h.sent), 1)
        h.run(down())
        self.assertEqual(len(h.sent), 1)

    def test_failed_recovery_send_retries_next_run(self):
        h = Harness()
        h.run(down()); h.run(down())
        h.send_ok = False
        h.run(up())
        h.send_ok = True
        h.run(up())
        self.assertEqual(len(h.sent), 2)
        self.assertIn("RECOVERED", h.sent[1])
        h.run(up())
        self.assertEqual(len(h.sent), 2)

    def test_both_down_is_one_combined_message(self):
        h = Harness()
        h.run(down(), down())
        h.run(down(), down())
        self.assertEqual(len(h.sent), 1)
        self.assertIn("bridge:", h.sent[0])
        self.assertIn("cockpit:", h.sent[0])

    def test_lost_state_starts_clean(self):
        h = Harness()
        h.run(down())
        Path(h.env["WATCHDOG_STATE"]).unlink()
        h.run(down())
        self.assertEqual(h.sent, [])
        self.assertEqual(h.state()["targets"]["bridge"]["fails"], 1)

    def test_test_alert_is_labelled_and_sent(self):
        h = Harness()
        h.run(up(), WATCHDOG_TEST_ALERT="true")
        self.assertEqual(len(h.sent), 1)
        self.assertTrue(h.sent[0].startswith("[TEST] hub watchdog wiring"))
        h.run(up(), WATCHDOG_TEST_ALERT="false")
        self.assertEqual(len(h.sent), 1)

    def test_no_targets_is_not_a_blind_pass(self):
        rc = w.main(env={"WATCHDOG_STATE": "/nonexistent/x.json"}, probe_fn=None, send_fn=None)
        self.assertEqual(rc, 2)

    def test_messages_and_state_carry_no_url(self):
        h = Harness()
        h.run(down()); h.run(down()); h.run(up())
        blob = "\n".join(h.sent) + Path(h.env["WATCHDOG_STATE"]).read_text()
        self.assertNotIn("example", blob)


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        status, headers, body = self.routes[self.path]
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Handler.routes = {
            "/ok": (200, {}, '{"ok": true}'),
            "/login": (200, {}, "<html>Sign in</html>"),
            "/redirect": (302, {"Location": "/ok"}, ""),
            "/sha": (200, {}, '{"sha": "3d5de017abc"}'),
            "/short": (200, {}, '{"sha": "3d5"}'),
            "/boom": (503, {}, "down"),
        }
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def check(self, path, target):
        healthy = next(t["healthy"] for t in w.TARGETS if t["name"] == target)
        return w.probe(self.base + path, healthy, sleep=lambda s: None)

    def test_bridge_ok(self):
        self.assertTrue(self.check("/ok", "bridge")["ok"])

    def test_login_page_200_is_down(self):
        r = self.check("/login", "bridge")
        self.assertFalse(r["ok"])
        self.assertIn("unexpected body", r["why"])

    def test_redirect_is_not_followed(self):
        r = self.check("/redirect", "bridge")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 302)

    def test_cockpit_sha(self):
        self.assertTrue(self.check("/sha", "cockpit")["ok"])
        self.assertFalse(self.check("/short", "cockpit")["ok"])

    def test_5xx_is_down_after_retry(self):
        r = self.check("/boom", "bridge")
        self.assertFalse(r["ok"])
        self.assertEqual(r["why"], "HTTP 503 (twice)")

    def test_connection_refused_is_down(self):
        r = w.probe("http://127.0.0.1:9/x", lambda b: True, sleep=lambda s: None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 0)


if __name__ == "__main__":
    unittest.main()
