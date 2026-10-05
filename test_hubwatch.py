"""Hermetic tests for hubwatch.py: no network, no sleeping. Run: python3 -m unittest -v test_hubwatch"""
import calendar
import contextlib
import io
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
    """Drives main() run after run with scripted probe results, a fake sender and a
    fake token check. Runs are 300 s apart unless a run says otherwise."""

    def __init__(self, now=1_000_000.0):
        self.dir = tempfile.TemporaryDirectory()
        self.sent: list[str] = []
        self.send_ok = True
        self.token_ok = True        # what the fake getMe answers: True, False or None
        self.token_calls = 0
        self.rcs: list[int] = []
        self.now = now
        self.env = {"WATCH_BRIDGE_URL": "https://b.example/health",
                    "WATCH_COCKPIT_URL": "https://c.example/api/version",
                    "WATCHDOG_STATE": str(Path(self.dir.name) / "state" / "state.json")}

    def run(self, bridge, cockpit=None, step_s=300, **extra):
        cockpit = cockpit or up()
        by_url = {self.env["WATCH_BRIDGE_URL"]: bridge, self.env["WATCH_COCKPIT_URL"]: cockpit}

        def send(text, env):
            if self.send_ok:
                self.sent.append(text)
            return self.send_ok

        def token(env):
            self.token_calls += 1
            return self.token_ok

        with contextlib.redirect_stdout(io.StringIO()):
            rc = w.main(env={**self.env, **extra}, now=self.now,
                        probe_fn=lambda url, healthy: dict(by_url[url]), send_fn=send, token_fn=token)
        self.now += step_s
        self.rcs.append(rc)
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

    def test_two_strikes_need_the_time_floor(self):
        # A cron run folded right behind a chain run: two failed runs 15 s apart are
        # one strike, so the page waits for a failure at least MIN_DOWN_S after the first.
        h = Harness()
        h.run(down(), step_s=15)
        h.run(down(), step_s=300)
        self.assertEqual(h.sent, [])
        h.run(down())
        self.assertEqual(len(h.sent), 1)
        self.assertIn("3 checks in a row", h.sent[0])

    def test_recovery_waits_for_three_healthy_runs(self):
        h = Harness()
        h.run(down()); h.run(down()); h.run(down())
        h.run(up()); h.run(up())
        self.assertEqual(len(h.sent), 1, "two healthy runs are not a recovery yet")
        self.assertTrue(h.state()["targets"]["bridge"]["alerted"])
        h.run(up())
        self.assertEqual(len(h.sent), 2)
        self.assertIn("RECOVERED", h.sent[1])
        self.assertIn("bridge is answering again", h.sent[1])
        h.run(up())
        self.assertEqual(len(h.sent), 2)
        self.assertFalse(h.state()["targets"]["bridge"]["alerted"])

    def test_flapping_target_pages_once_in_two_hours(self):
        # fail, fail, ok, repeated every ~5.3 min: the old code sent 14 messages here.
        h = Harness()
        pattern = [down(), down(), up()]
        for i in range(int(120 / 5.3)):
            h.run(pattern[i % 3], step_s=318)
        self.assertEqual(len(h.sent), 1)
        self.assertIn("DOWN", h.sent[0])

    def test_recovered_reports_the_whole_outage_through_a_dip(self):
        h = Harness(now=0.0)
        h.run(down()); h.run(down())          # t=0 first failure, page at t=300
        h.run(up()); h.run(down())            # t=600 back, t=900 dips again: still the same outage
        h.run(up()); h.run(up()); h.run(up())  # t=1200 back for good; RECOVERED at t=1800
        self.assertEqual(len(h.sent), 2)
        self.assertIn("it was down for about 20 min", h.sent[1])

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
        h.run(up()); h.run(up())
        h.send_ok = False
        h.run(up())                           # RECOVERED is due here and fails
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
        h.run(up(), WATCHDOG_TEST_ALERT="true", WATCHDOG_TEST_LABEL="after  fixes\n")
        self.assertTrue(h.sent[1].startswith("[TEST] hub watchdog wiring (after fixes) - "), h.sent[1])

    def test_no_targets_is_not_a_blind_pass(self):
        rc = w.main(env={"WATCHDOG_STATE": "/nonexistent/x.json"}, probe_fn=None, send_fn=None)
        self.assertEqual(rc, 2)

    def test_messages_and_state_carry_no_url(self):
        h = Harness()
        h.run(down()); h.run(down()); h.run(up()); h.run(up()); h.run(up())
        blob = "\n".join(h.sent) + Path(h.env["WATCHDOG_STATE"]).read_text()
        self.assertNotIn("example", blob)


class AlertPathTests(unittest.TestCase):
    """Exit 3 whenever the watchdog could not reach JD: a mute watchdog must fail its run."""

    def test_quiet_healthy_run_exits_0(self):
        h = Harness()
        self.assertEqual(h.run(up()), 0)

    def test_outage_page_that_cannot_be_sent_exits_3_until_it_goes_out(self):
        h = Harness()
        self.assertEqual(h.run(down()), 0)
        h.send_ok = False
        self.assertEqual(h.run(down()), 3)
        self.assertEqual(h.run(down()), 3)
        h.send_ok = True
        self.assertEqual(h.run(down()), 0)
        self.assertEqual(h.run(down()), 0)

    def test_recovery_that_cannot_be_sent_exits_3(self):
        h = Harness()
        h.run(down()); h.run(down()); h.run(up()); h.run(up())
        h.send_ok = False
        self.assertEqual(h.run(up()), 3)

    def test_test_alert_that_cannot_be_sent_exits_3(self):
        h = Harness()
        h.send_ok = False
        self.assertEqual(h.run(up(), WATCHDOG_TEST_ALERT="true"), 3)


class TokenCheckTests(unittest.TestCase):
    NINE = calendar.timegm((2026, 10, 5, 9, 3, 0))    # inside the 09:00-09:09 UTC window

    def test_when_the_check_is_due(self):
        nine, hour = self.NINE, 3600
        self.assertTrue(w.token_check_due({"token_ok_at": nine - 60}, {}, nine), "daily window")
        self.assertFalse(w.token_check_due({"token_ok_at": nine - 60}, {}, nine + hour))
        self.assertTrue(w.token_check_due({}, {}, nine + hour), "never checked")
        self.assertTrue(w.token_check_due({"token_ok_at": nine - 27 * hour}, {}, nine + hour), "window missed")
        self.assertTrue(w.token_check_due({"token_ok_at": nine, "token_bad_since": nine}, {}, nine + hour))
        self.assertTrue(w.token_check_due({"token_ok_at": nine}, {"WATCHDOG_CHECK_TOKEN": "true"}, nine + hour))

    def test_daily_check_runs_in_the_window_and_not_between(self):
        h = Harness(now=self.NINE - 600)          # 08:53: first run checks (never checked before)
        h.run(up())
        self.assertEqual(h.token_calls, 1)
        h.run(up())                               # 08:58: fresh, outside the window
        self.assertEqual(h.token_calls, 1)
        h.run(up())                               # 09:03: the daily window
        self.assertEqual(h.token_calls, 2)
        h.run(up()); h.run(up())                  # 09:08 in, 09:13 out
        self.assertEqual(h.token_calls, 3)

    def test_rejected_token_fails_every_run_until_it_is_accepted(self):
        h = Harness()
        h.token_ok = False
        self.assertEqual(h.run(up()), 3)
        self.assertEqual(h.run(up()), 3)          # outside the window: re-checked because it is bad
        self.assertEqual(h.token_calls, 2)
        h.token_ok = True
        self.assertEqual(h.run(up()), 0)
        self.assertEqual(h.run(up()), 0)
        self.assertEqual(h.token_calls, 3, "a good token is not re-checked every run")

    def test_inconclusive_check_keeps_a_known_bad_token_failing(self):
        h = Harness()
        h.token_ok = False
        self.assertEqual(h.run(up()), 3)
        h.token_ok = None
        self.assertEqual(h.run(up()), 3)
        h.token_ok = True
        self.assertEqual(h.run(up()), 0)

    def test_inconclusive_check_on_an_unknown_token_does_not_fail_but_asks_again(self):
        h = Harness()
        h.token_ok = None
        self.assertEqual(h.run(up()), 0)
        self.assertEqual(h.run(up()), 0)
        self.assertEqual(h.token_calls, 2)

    def test_token_check_classifies_the_bot_api_answer(self):
        cases = {200: True, 401: False, 404: False, 429: None, 502: None, 0: None}
        orig = w.bot_api
        try:
            for status, want in cases.items():
                w.bot_api = lambda token, method, payload=None, s=status: (s, f"HTTP {s}")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIs(w.token_check({"TELEGRAM_BOT_TOKEN": "123:abc"}), want, status)
            w.bot_api = lambda *a, **k: self.fail("a missing token must not be sent anywhere")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIs(w.token_check({}), False)
        finally:
            w.bot_api = orig

    def test_bot_api_never_prints_or_returns_the_token(self):
        secret = "123456:SECRET-TOKEN-VALUE"
        orig = w._bot_post

        def boom(token, method, data):
            raise OSError(f"cannot reach https://host/bot{token}/{method}")
        w._bot_post = boom
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status, why = w.bot_api(secret, "getMe")
                sent = w.telegram("hello", {"TELEGRAM_BOT_TOKEN": secret, "TELEGRAM_CHAT_ID": "1"})
            self.assertEqual((status, why, sent), (0, "OSError", False))
            self.assertNotIn("SECRET", out.getvalue() + why)
        finally:
            w._bot_post = orig


class StateFileTests(unittest.TestCase):
    def test_save_is_atomic_and_leaves_no_temp_file(self):
        d = Path(tempfile.mkdtemp())
        path = d / "state" / "state.json"
        w.save_state(path, {"targets": {"bridge": {"fails": 1}}})
        with self.assertRaises(TypeError):
            w.save_state(path, {"targets": object()})     # dies mid-write
        self.assertEqual(json.loads(path.read_text()), {"targets": {"bridge": {"fails": 1}}})
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["state.json"])

    def test_a_torn_or_foreign_state_file_reads_as_a_first_run(self):
        d = Path(tempfile.mkdtemp())
        for text in ('{"targets": {"bri', "[1, 2]"):
            (d / "s.json").write_text(text)
            self.assertEqual(w.load_state(d / "s.json"), {"targets": {}})


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

    def test_malformed_url_secret_is_down_not_a_crash(self):
        r = w.probe("bridge.example/health", lambda b: True, sleep=lambda s: None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["why"], "ValueError (twice)")


if __name__ == "__main__":
    unittest.main()
