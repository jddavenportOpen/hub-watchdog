#!/usr/bin/env python3
"""Two-strike uptime watchdog, run on a GitHub Actions schedule.

Probes a small set of HTTPS endpoints from outside the host it watches and sends
ONE Telegram message when a target has failed two runs in a row, and ONE more when
it answers again. Standard library only.

Configuration (all from the environment, normally repository secrets):
    WATCH_<NAME>_URL     one per target in TARGETS below; a target with no URL is skipped
    TELEGRAM_BOT_TOKEN   bot that sends the alert
    TELEGRAM_CHAT_ID     chat that receives it
    WATCHDOG_STATE       path of the JSON state file (persisted between runs by actions/cache)
    WATCHDOG_TEST_ALERT  "true" sends a single "[TEST] hub watchdog wiring" message this run

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
RETRY_DELAY_S = 20          # a failed probe is retried once inside the run
TIMEOUT_S = 15
USER_AGENT = "hub-watchdog-gha/1"


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
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    try:
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
    outage: down when fails first reaches FAIL_THRESHOLD and no alert has gone out,
    up when an alerted target answers again. The caller marks `alerted` only after
    a send succeeded, through `commit_alerts`.
    """
    targets = dict(state.get("targets") or {})
    down, up = [], []
    for name, r in results.items():
        s = dict(targets.get(name) or {"fails": 0, "alerted": False, "since": 0})
        if r["ok"]:
            if s.get("alerted"):
                up.append(f"{name} is answering again (HTTP {r['status']}); "
                          f"it was down for about {fmt_minutes(now - s.get('since', now))}.")
            targets[name] = {"fails": 0, "alerted": False, "since": 0}
            if s.get("alerted"):
                targets[name]["pending_recovery_since"] = s.get("since", now)
            continue
        s["fails"] = int(s.get("fails", 0)) + 1
        if s["fails"] == 1:
            s["since"] = now
        if s["fails"] >= FAIL_THRESHOLD and not s.get("alerted"):
            down.append(f"{name}: {r.get('why', 'no answer')}; failing for "
                        f"{fmt_minutes(now - s['since'])} ({s['fails']} checks in a row).")
            s["pending_alert"] = True
        targets[name] = s
    return {**state, "targets": targets}, down, up


def commit_alerts(state: dict, sent: bool) -> dict:
    """Mark pending messages delivered, or leave them pending to retry next run.

    A failed DOWN send leaves `alerted` False, so the next failing run retries it.
    A failed RECOVERY send restores `alerted`, so the next healthy run retries it.
    """
    for s in state.get("targets", {}).values():
        if s.pop("pending_alert", False) and sent:
            s["alerted"] = True
        since = s.pop("pending_recovery_since", None)
        if since is not None and not sent:
            s["alerted"], s["since"] = True, since
    return state


def compose(down: list[str], up: list[str]) -> str:
    parts = []
    if down:
        parts.append("Hub watchdog (GitHub Actions, outside the hub): DOWN\n- " + "\n- ".join(down)
                     + "\nThe hub, its tunnel or its network may be down. One message per outage; "
                       "you will get another when it recovers.")
    if up:
        parts.append("Hub watchdog: RECOVERED\n- " + "\n- ".join(up))
    return "\n\n".join(parts)


def telegram(text: str, env: dict) -> bool:
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("telegram: skipped, secrets missing")
        return False
    data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            ok = 200 <= r.status < 300
            print(f"telegram: HTTP {r.status}")
            return ok
    except urllib.error.HTTPError as e:
        print(f"telegram: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 - never echo the exception text: it can carry the URL
        print(f"telegram: {type(e).__name__}")
    return False


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - first run, or the cache was evicted
        return {"targets": {}}


def main(env: dict | None = None, now: float | None = None,
         probe_fn: Callable[[str, Callable[[str], bool]], dict] = probe,
         send_fn: Callable[[str, dict], bool] = telegram) -> int:
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
    sent = True
    if down or up:
        sent = send_fn(compose(down, up), env)
        print(f"alert: down={len(down)} up={len(up)} sent={sent}")
    state = commit_alerts(state, sent)

    if env.get("WATCHDOG_TEST_ALERT", "").lower() == "true":
        summary = ", ".join(f"{n} {'OK' if r['ok'] else 'DOWN'}" for n, r in results.items())
        ok = send_fn("[TEST] hub watchdog wiring - the GitHub Actions watchdog outside the hub can "
                     f"reach you. {summary}. No action needed; this is sent once.", env)
        print(f"test alert: sent={ok}")

    state["last_run"] = now
    state["last_results"] = {n: {k: r[k] for k in ("ok", "status", "ms")} for n, r in results.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))
    print("fails: " + ", ".join(f"{n}={s.get('fails', 0)}" for n, s in sorted(state["targets"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
