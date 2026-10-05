# hub-watchdog

A two-strike uptime watchdog that runs on GitHub Actions, so it keeps working when
the machine it watches is down, asleep or off the network.

About every 5 minutes it probes a small set of HTTPS endpoints. When a target fails
two runs in a row it sends **one** Telegram message, and **one** more once the
target has answered three runs in a row. Nothing in between, even if the target
flaps.

## How it keeps time

GitHub's `schedule` trigger can drop scheduled runs or start them hours late, so
the cadence comes from a self-dispatch chain instead:

- every run ends by dispatching the next one with `wait=true`;
- that run first waits in the `tick` environment, whose **5-minute wait timer**
  holds no runner while it waits, and then probes;
- a concurrency group keeps at most one running and one pending run, so any extra
  dispatch (by hand, by the cron backstop, by an outside kicker) folds into the
  same chain;
- the cron schedule stays as a backstop that restarts a chain that broke.

Two guards stop a runaway chain. Only the repository named in the `if:` of the
"Dispatch the next tick" step chains at all. And a `wait=true` run that probed
less than 240 seconds after it was created had no wait timer behind it, so it
refuses to dispatch the next run and fails instead.

## Why two strikes, a time floor, and a body check

- A failed probe is retried once, 20 seconds later, inside the same run. A run
  counts as failed only when both attempts fail.
- An alert needs two failed runs in a row, and the first of them at least 240
  seconds earlier, so two runs that start back to back count as one strike.
- RECOVERED needs three healthy runs in a row. A target that flaps stays "down"
  and sends nothing more until it holds, and the RECOVERED message reports the
  whole outage.
- A `200` alone proves nothing: a login wall, a parked domain or a CDN error page
  can all answer `200`. Each target names the JSON body it must return, and
  redirects are not followed.
- Expect the alert 6 to 11 minutes after a target goes down.

## A mute watchdog fails loudly

`hubwatch.py` exits `3`, which fails the run, when the alert path is broken: a
message that was due could not be sent, or the daily token check found the bot
token rejected. That check calls the Bot API `getMe`, which sends nothing, at
09:00-09:09 UTC. Once the token has been rejected, every run checks again and fails
until it is accepted, so a dead token shows up as a run of failures rather than a
row of green checks. Exit `2` means no target is configured.

## Setup

Order matters: create the `tick` environment **before** the first dispatch. A
chained run that finds no wait timer stops the chain (see the guards above).

1. Copy this repository. In the copy, put your own `owner/repo` in the `if:` of
   the "Dispatch the next tick" step; until you do, the chain does not run there.
2. Create the environment `tick` with a 5-minute wait timer (Settings,
   Environments, New environment, Wait timer = 5), or from a shell:

   ```
   gh api -X PUT repos/OWNER/REPO/environments/tick -F wait_timer=5
   ```

3. Add these repository secrets (Settings, Secrets and variables, Actions):

   | Secret | Value |
   |---|---|
   | `WATCH_BRIDGE_URL` | a URL that answers `{"ok": true}` |
   | `WATCH_COCKPIT_URL` | a URL that answers `{"sha": "<at least 7 chars>"}` |
   | `TELEGRAM_BOT_TOKEN` | the bot that sends the alert |
   | `TELEGRAM_CHAT_ID` | the chat that receives it |

   A target with no URL is skipped. With no targets at all the run fails rather
   than reporting a blind pass. The URLs are secrets so the public run logs never
   show them.
4. Run the `watchdog` workflow once by hand with **test_alert** checked. You get a
   single `[TEST] hub watchdog wiring` message, which proves the alert path, and
   that run starts the chain. An optional **test_label** is shown in the message's
   label, for example `[TEST] hub watchdog wiring (after fixes)`.

To check a new bot token without sending anything, run the workflow by hand with
**check_token** checked. To watch other endpoints, edit `TARGETS` in `hubwatch.py`.

## How state survives between runs

State (consecutive failures, whether an alert went out, the last good token check)
is a small JSON file kept in the Actions cache. Each run restores the newest copy,
writes the file atomically, saves a new copy and prunes all but the three newest
copies on the default branch. If the cache is ever lost, the watchdog starts
counting from zero: an outage already in progress pages again once it has failed
two more runs, and a RECOVERED message that was still pending is never sent.

A daily run (09:00-09:09 UTC) calls the "enable workflow" API so GitHub's 60-day
inactivity rule does not switch the workflow off.

## Tests

```
python3 -m unittest -v test_hubwatch
```

Standard library only. The tests use a local HTTP server, a fake sender and a fake
token check, so they need no network and no secrets.

## License

MIT
