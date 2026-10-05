#!/usr/bin/env python3
"""Two-strike uptime watchdog, run every few minutes on GitHub Actions.

Probes a small set of HTTPS endpoints from outside the host it watches and sends
ONE Telegram message when a target has failed two runs in a row (at least
MIN_DOWN_S apart), and ONE more once it has answered RECOVER_THRESHOLD runs in a
row. A target that flaps stays "down" until it holds, so it cannot spam.
Standard library only.

Configuration (all from the environment, normally repository secrets):
    WATCH_<NAME>_URL      one per target in TARGETS below; a target with no URL is skipped
    TELEGRAM_BOT_TOKEN    bot that sends the alert
    TELEGRAM_CHAT_ID      chat that receives it
    WATCHDOG_STATE        path of the JSON state file (persisted between runs by actions/cache)
    WATCHDOG_TEST_ALERT   "true" sends a single "[TEST] hub watchdog wiring" message this run
    WATCHDOG_TEST_LABEL   optional note shown in that message's label, e.g. "(after fixes)"
    WATCHDOG_CHECK_TOKEN  "true" verifies the bot token this run (getMe, which sends nothing)

Exit codes:
    0  ran; any message that was due went out
    2  no target configured (refuses to report a blind pass)
    3  the alert path is broken: a message that was due could not be sent, or the
       bot token was rejected by the daily check. The run fails on purpose, so a
       mute watchdog is visible in the run list instead of looking healthy.

The run log is public on a public repository, so this script never prints a URL, a
response body or a message text: only target names, HTTP status codes and timings.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

FAIL_THRESHOLD = 2          # consecutive failed RUNS before the one alert
MIN_DOWN_S = 240            # ...and the first of them at least this long ago: two runs
                            # folded together (a cron run right behind a chain run) are one strike
RECOVER_THRESHOLD = 3       # consecutive healthy RUNS before RECOVERED; until then the
                            # target stays alerted, so fail-fail-ok-fail-fail never re-pages
RETRY_DELAY_S = 20          # a failed probe is retried once inside the run
TIMEOUT_S = 15
USER_AGENT = "hub-watchdog-gha/1"
TOKEN_CHECK_MAX_AGE_S = 26 * 3600   # verify the bot token at least this often...
TOKEN_CHECK_UTC_HOUR = 9            # ...and in this daily window (minutes 0-9, the keepalive window)
EXIT_ALERT_PATH_BROKEN = 3


def _json_field(body: str, pred: Callable[[object], bool]) -> bool:
    try:
        return bool(pred(json.loads(body)))
    except Exception:  # noqa: BLE001 - any parse failure is "not healthy"
        return False


# A 2xx alone is a blind pass: a login wall, a parked page or a CDN error page can
# answer 200 too. Each target names the body it must see. Redirects are not followed.
TARGETS: list[dict] = [
    {"name": "bridge", "env": "WATCH_BRIDGE_URL",
     "healthy": lambda b: _json_field(b, lambda j: isinstance(j, dict) and j.get("ok") is True)},
    {"name": "cockpit", "env": "WATCH_COCKPIT_URL",
     "healthy": lambda b: _json_field(
         b, lambda j: isinstance(j, dict) and isinstance(j.get("sha"), str) and len(j["sha"]) >= 7)},
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def probe_once(url: str, healthy: Callable[[str], bool]) -> dict:
    started = time.monotonic()
    try:
        # Inside the try: a malformed URL secret raises ValueError here, and that is a
        # DOWN target ("ValueError"), not a crashed run that never pages anyone.
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
        with _OPENER.open(req, timeout=TIMEOUT_S) as r:
            status = r.status
            body = r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "ms": _ms(started), "why": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - DNS, TLS, timeout, reset: all "no answer"
        return {"ok": False, "status": 0, "ms": _ms(started), "why": type(e).__name__}
    if not 200 <= status < 300:
        return {"ok": False, "status": status, "ms": _ms(started), "why": f"HTTP {status}"}
    if not healthy(body):
        return {"ok": False, "status": status, "ms": _ms(started), "why": f"HTTP {status}, unexpected body"}
    return {"ok": True, "status": status, "ms": _ms(started)}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def probe(url: str, healthy: Callable[[str], bool], sleep: Callable[[float], None] = time.sleep) -> dict:
    """One probe, retried once after RETRY_DELAY_S. A run fails only if both fail."""
    first = probe_once(url, healthy)
    if first["ok"]:
        return first
    sleep(RETRY_DELAY_S)
    second = probe_once(url, healthy)
    if not second["ok"]:
        second["why"] = f"{second['why']} (twice)"
    return second


def fmt_minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def step(state: dict, results: dict[str, dict], now: float) -> tuple[dict, list[str], list[str]]:
    """Advance the per-target state with this run's results.

    Returns (new_state, down_lines, up_lines). A line appears at most once per
    outage:
      - down when a target has failed FAIL_THRESHOLD runs in a row, the first of
        them at least MIN_DOWN_S ago, and no alert has gone out;
      - up when an alerted target has answered RECOVER_THRESHOLD runs in a row.
    An alerted target that fails again before that stays alerted and keeps the
    outage's original `since`, so a flapping target sends nothing more and the
    eventual RECOVERED reports the whole outage. The caller marks a message
    delivered only after the send succeeded, through `commit_alerts`.
    """
    targets = dict(state.get("targets") or {})
    down, up = [], []
    for name, r in results.items():
        s = dict(targets.get(name) or {})
        alerted = bool(s.get("alerted"))
        if r["ok"]:
            if not alerted:
                targets[name] = {"fails": 0, "alerted": False, "since": 0}
                continue
            s["fails"] = 0
            s["oks"] = int(s.get("oks", 0)) + 1
            if s["oks"] == 1 or "up_since" not in s:
                s["up_since"] = now
            if s["oks"] >= RECOVER_THRESHOLD:
                down_for = s["up_since"] - float(s.get("since", s["up_since"]))
                up.append(f"{name} is answering again (HTTP {r['status']}, {s['oks']} checks in a row); "
                          f"it was down for about {fmt_minutes(down_for)}.")
                s["pending_recovery"] = True
            targets[name] = s
            continue
        s["fails"] = int(s.get("fails", 0)) + 1
        s.pop("oks", None)
        s.pop("up_since", None)
        if not alerted:
            s["alerted"] = False
            if s["fails"] == 1 or "since" not in s:
                s["since"] = now
            if s["fails"] >= FAIL_THRESHOLD and now - s["since"] >= MIN_DOWN_S:
                down.append(f"{name}: {r.get('why', 'no answer')}; failing for "
                            f"{fmt_minutes(now - s['since'])} ({s['fails']} checks in a row).")
                s["pending_alert"] = True
        targets[name] = s
    return {**state, "targets": targets}, down, up


def commit_alerts(state: dict, sent: bool) -> dict:
    """Mark pending messages delivered, or leave them pending to retry next run.

    A failed DOWN send leaves `alerted` False, so the next failing run retries it.
    A failed RECOVERED send leaves the target alerted with its healthy streak, so
    the next healthy run retries it, and a new failure simply resumes the outage.
    """
    targets = state.get("targets", {})
    for name in list(targets):
        s = targets[name]
        if s.pop("pending_alert", False) and sent:
            s["alerted"] = True
        if s.pop("pending_recovery", False) and sent:
            targets[name] = {"fails": 0, "alerted": False, "since": 0}
    return state


def compose(down: list[str], up: list[str]) -> str:
    parts = []
    if down:
        parts.append("Hub watchdog (GitHub Actions, outside the hub): DOWN\n- " + "\n- ".join(down)
                     + "\nThe hub, its tunnel or its network may be down. One message per outage; "
                       f"you will get another once it has answered {RECOVER_THRESHOLD} checks in a row.")
    if up:
        parts.append("Hub watchdog: RECOVERED\n- " + "\n- ".join(up))
    return "\n\n".join(parts)


def bot_api(token: str, method: str, payload: dict | None = None) -> tuple[int, str]:
    """One Bot API call: (HTTP status, 0 for no answer; a printable reason).

    Never prints or returns the URL or an exception's text: both can carry the token.
    """
    try:
        return _bot_post(token, method, json.dumps(payload or {}).encode())
    except urllib.error.HTTPError as e:
        return e.code, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - DNS, TLS, timeout, reset: all "no answer"
        return 0, type(e).__name__


def _bot_post(token: str, method: str, data: bytes) -> tuple[int, str]:
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.status, f"HTTP {r.status}"


def telegram(text: str, env: dict) -> bool:
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("telegram: skipped, secrets missing")
        return False
    status, why = bot_api(token, "sendMessage",
                          {"chat_id": chat, "text": text, "disable_web_page_preview": True})
    print(f"telegram: {why}")
    return 200 <= status < 300


def token_check(env: dict) -> bool | None:
    """Is the bot token still accepted? getMe sends nothing to anyone.

    True: accepted. False: rejected (HTTP 401/404) or missing, so every alert would
    fail. None: could not tell (no answer, 429, 5xx); the next run asks again.
    """
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("telegram token check: secret missing")
        return False
    status, why = bot_api(token, "getMe")
    print(f"telegram token check: {why}")
    if 200 <= status < 300:
        return True
    if status in (401, 404):
        return False
    return None


def token_check_due(state: dict, env: dict, now: float) -> bool:
    """Daily in the keepalive window (09:00-09:09 UTC), on demand, on every run while
    the token is known bad, and whenever the last good check is older than
    TOKEN_CHECK_MAX_AGE_S (a missed window, or a fresh state)."""
    if env.get("WATCHDOG_CHECK_TOKEN", "").lower() == "true" or state.get("token_bad_since"):
        return True
    t = time.gmtime(now)
    if t.tm_hour == TOKEN_CHECK_UTC_HOUR and t.tm_min < 10:
        return True
    return now - float(state.get("token_ok_at") or 0) >= TOKEN_CHECK_MAX_AGE_S


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {"targets": {}}
    except Exception:  # noqa: BLE001 - first run, or the cache was evicted
        return {"targets": {}}


def save_state(path: Path, state: dict) -> None:
    """Write-then-rename: a run killed mid-write never leaves a torn file that the
    next run would read as a first run (and so forget an outage in progress)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main(env: dict | None = None, now: float | None = None,
         probe_fn: Callable[[str, Callable[[str], bool]], dict] = probe,
         send_fn: Callable[[str, dict], bool] = telegram,
         token_fn: Callable[[dict], bool | None] = token_check) -> int:
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    path = Path(env.get("WATCHDOG_STATE", "state/state.json"))
    state = load_state(path)

    results: dict[str, dict] = {}
    for t in TARGETS:
        url = env.get(t["env"], "").strip()
        if not url:
            print(f"{t['name']}: no URL configured, skipped")
            continue
        r = probe_fn(url, t["healthy"])
        results[t["name"]] = r
        print(f"{t['name']}: {'UP' if r['ok'] else 'DOWN'} status={r['status']} ms={r['ms']}"
              + ("" if r["ok"] else f" ({r.get('why', '')})"))

    if not results:
        print("no targets configured: refusing to report a blind pass")
        return 2

    state, down, up = step(state, results, now)
    broken: list[str] = []      # each entry fails this run (exit 3): a mute watchdog must look broken
    sent = True
    if down or up:
        sent = send_fn(compose(down, up), env)
        print(f"alert: down={len(down)} up={len(up)} sent={sent}")
        if not sent:
            broken.append("a message that was due could not be sent; the next run retries it")
    state = commit_alerts(state, sent)

    if env.get("WATCHDOG_TEST_ALERT", "").lower() == "true":
        summary = ", ".join(f"{n} {'OK' if r['ok'] else 'DOWN'}" for n, r in results.items())
        label = " ".join(env.get("WATCHDOG_TEST_LABEL", "").split())[:60]
        tag = f"[TEST] hub watchdog wiring ({label})" if label else "[TEST] hub watchdog wiring"
        ok = send_fn(f"{tag} - the GitHub Actions watchdog outside the hub can "
                     f"reach you. {summary}. No action needed; this is sent once.", env)
        print(f"test alert: sent={ok}")
        if not ok:
            broken.append("the test alert could not be sent")

    # A dead token is invisible until the outage it fails to report, so ask Telegram
    # daily whether the token still works. Once it has been rejected, every run asks
    # again and fails until it is accepted, so the failure persists until fixed.
    if token_check_due(state, env, now):
        verdict = token_fn(env)
        if verdict is True:
            state["token_ok_at"] = now
            state.pop("token_bad_since", None)
        elif verdict is False:
            state.setdefault("token_bad_since", now)
    if state.get("token_bad_since"):
        broken.append("the Telegram bot token was rejected (or is not set); every alert would fail")

    state["last_run"] = now
    state["last_results"] = {n: {k: r[k] for k in ("ok", "status", "ms")} for n, r in results.items()}
    save_state(path, state)
    print("fails: " + ", ".join(f"{n}={s.get('fails', 0)}" for n, s in sorted(state["targets"].items())))
    for why in broken:
        print(f"::error::alert path broken: {why}")
    return EXIT_ALERT_PATH_BROKEN if broken else 0


if __name__ == "__main__":
    sys.exit(main())
