# hub-watchdog

A two-strike uptime watchdog that runs on a GitHub Actions schedule, so it keeps
working when the machine it watches is down, asleep or off the network.

Every 5 minutes it probes a small set of HTTPS endpoints. When a target fails two
runs in a row it sends **one** Telegram message, and **one** more when the target
answers again. Nothing in between.

## Why two strikes, and why a body check

- A single failed probe is retried once, 20 seconds later, inside the same run. A
  run counts as failed only when both attempts fail, and an alert needs two failed
  runs in a row. Short blips never page.
- A `200` alone proves nothing: a login wall, a parked domain or a CDN error page
  can all answer `200`. Each target names the JSON body it must return, and
  redirects are not followed.
- GitHub may start a scheduled run several minutes late, so expect detection in
  roughly 10 to 20 minutes, not 10.

## Setup

1. Copy this repository. The schedule runs from the default branch. In a fork,
   scheduled workflows stay off until you enable Actions there.
2. Add these repository secrets (Settings, Secrets and variables, Actions):

   | Secret | Value |
   |---|---|
   | `WATCH_BRIDGE_URL` | a URL that answers `{"ok": true}` |
   | `WATCH_COCKPIT_URL` | a URL that answers `{"sha": "<at least 7 chars>"}` |
   | `TELEGRAM_BOT_TOKEN` | the bot that sends the alert |
   | `TELEGRAM_CHAT_ID` | the chat that receives it |

   A target with no URL is skipped. With no targets at all the run fails rather
   than reporting a blind pass. The URLs are secrets so the public run logs never
   show them.
3. Run the `watchdog` workflow once by hand with **test_alert** checked. You get a
   single `[TEST] hub watchdog wiring` message, which proves the alert path.

To watch other endpoints, edit `TARGETS` in `hubwatch.py`.

## How state survives between runs

State (consecutive failures, whether an alert went out) is a small JSON file kept in
the Actions cache: each run restores the newest copy, saves a new one and prunes all
but the three newest. If the cache is ever lost the watchdog simply starts counting
again; the worst case is one repeated alert.

A weekly run calls the "enable workflow" API so GitHub's 60-day inactivity rule does
not silently switch the schedule off.

## Tests

```
python3 -m unittest -v test_hubwatch
```

Standard library only. The tests use a local HTTP server and a fake sender, so they
need no network and no secrets.

## License

MIT
