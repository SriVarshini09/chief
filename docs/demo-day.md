# Demo day: preflight, failover, and what to do when the Wi-Fi drops

The script itself is in [demo-script.md](demo-script.md). This page is about keeping it alive.

## The modes

| `CHIEF_MODE` / `--mode` | Reads | Writes (calendar, send, drafts, Slack) | Use it for |
|---|---|---|---|
| `mock` | seeded sample data | seeded sample data | rehearsing the script, tests |
| `live` | real Gmail, Calendar, Parallel | real, after approval | first checks with real keys |
| `demo` | **live first**; if a read fails or takes longer than `CHIEF_LIVE_TIMEOUT` (6 s) it is answered from the cache | **live only**. If live is down the write fails with a clear error. Never replayed, never faked | the real thing on stage |
| `demo-offline` | the cache only, no network at all | **simulated** by the mock and tagged `simulated`; nothing real is touched | rehearsing with the Wi-Fi off, or the last-resort fallback |

`python -m chief demo` runs `demo` by default; `--mode` picks another.

### How failover behaves (`demo`)

- Every read goes to the real service first, on a worker thread with a timeout. Any exception or timeout switches **that call** to the cache.
- After a failure Chief stops waiting on the network for 30 seconds (the "breaker"), so five reads in a row do not each burn six seconds. It probes the live service again after that.
- Anything answered from the cache is **never presented as live**: the activity event carries `detail.cached = true` (plus `cached_at`, `cache_match`, and the reason), its title ends in "(cached)", and the Stage shows a small `cached` tag on the panel.
- `cache_match` says how close the answer is: `exact` (same call), `derived` (rebuilt from recorded data, e.g. the calendar for a different time window), `similar` (a web search worded almost the same).
- A **write is never replayed or faked**. If you say "do it" while the network is down, the activity stream shows `... failed: ... It was NOT done and was not faked.` and Chief says "That didn't go through". A write that times out may still complete later, so the error tells you to check before trying again.
- A read with no cached answer at all fails with both reasons, so the stream says what to fix (`python -m chief record-demo`).

### How `demo-offline` behaves

- Reads come from `data/demo_cache.json`. The demo runs at a different time than the recording, so calendar windows are rebuilt from the recorded events, emails are filtered by the query, and web searches match on wording (clock times and dates in the request are ignored).
- Writes run against the mock and are tagged `simulated` (the title ends in "(simulated)"). The calendar you see afterwards shows the simulated changes on top of the recorded one, so "Do it" and "Add it" still look right. Your real calendar and inbox are not touched; **say so out loud**.
- Needs a cache. `python -m chief doctor` fails if there is none.
- `python -m chief demo --mode demo-offline` switches the language model off for the run (no network means no call that could hang), so summaries use Chief's built-in wording.

## Record the safety net (do this online, the evening before and again an hour before)

```powershell
.venv\Scripts\python.exe -m chief record-demo --dry-run   # shows the plan, calls nothing
.venv\Scripts\python.exe -m chief record-demo             # runs the story through the LIVE reads
```

It runs the phrases from the script (`STORY` in `chief/tools/replay.py`) with every write switched off: no draft is saved, nothing is sent or changed, and the two approval phrases are skipped. Each read result is saved to `data/demo_cache.json`, keyed by tool and arguments. Only the fields the skills use are kept; email text is only the snippets Chief already sees; event descriptions are dropped; key-like strings and `pwd=`/`token=` URL parameters are redacted. The old cache is kept as `demo_cache.json.bak`, and a recording that captured nothing never overwrites a good cache.

The cache contains snippets of the test account's mail and calendar titles. It is for your machine: do not commit it.

The recording only helps for what was recorded. Say the phrases as written; a new question will go live or, if the network is down, miss the cache.

## Preflight checklist

Run `python -m chief doctor` (add `--live` for one tiny call each to Parallel, the LLM and speech-to-text; `--offline` skips every network check). It prints `[ OK ]`, `[WARN]`, `[FAIL]` with a `fix:` line, shows only the *names* of keys that are set, and ends with "All critical checks passed" or the number of problems. `python -m chief demo` runs the same checks and refuses to start while a critical one fails (`--force` overrides).

| # | Check | Done when |
|---|---|---|
| 1 | Python 3.11+, running from `.venv` | doctor: `Python version` and `Virtual environment` OK |
| 2 | Packages | `pip install -e ".[live]"` once; doctor shows every package OK |
| 3 | Keys in `.env` | `COMPOSIO_API_KEY`, `PARALLEL_API_KEY` set (names only are shown). `CHIEF_LLM_*` and `CHIEF_STT_API_KEY` strongly recommended |
| 4 | Google test account connected | doctor: `Composio connections` lists gmail and googlecalendar as connected. If not: `python -m chief.tools.composio_client gmail` (and `calendar`), open the link signed in to the **test** account |
| 5 | Keys work | `python -m chief doctor --live` |
| 6 | Cache recorded | doctor: `Demo cache` OK, and recorded less than 24 h ago |
| 7 | `cloudflared` installed | doctor: OK. Otherwise `winget install --id Cloudflare.cloudflared`, then a new terminal. Chief never installs it for you |
| 8 | Port free | doctor: `Port 8765` OK |
| 9 | Browser found (only for `--show-windows`) | doctor: `Browser for real windows` shows a path |
| 10 | Full dress run | `python -m chief reset`, `python -m chief demo`, say all seven phrases from the phone, then once more with `--mode demo-offline` |
| 11 | Optional QR code | if `qrcode` or `segno` is already installed the phone link prints as a QR. Nothing installs it for you |

`python -m chief demo` then prints the Stage link (projector machine), the phone link (from the tunnel) and the seven phrases in order. **The phone link contains the access token: keep that terminal off the projector.**

## If the Wi-Fi drops

Stay calm; the system is built for this. Decide by what you see on the Stage:

1. **Reads still work, results show a `cached` tag, "Do it" says "That didn't go through".** You are in `demo` failover with live down. Tell the room: "The network is down, so Chief fell back to what it recorded earlier, and it is labelled. It will not fake a change." Keep going through the read-only beats (inbox, prep, trip). For the writes, switch to offline below.
2. **The phone page stops responding.** The phone reaches the laptop through the tunnel, so no Wi-Fi means no phone. Type the phrases into the page open on the laptop instead (`http://localhost:8765/?token=...`, printed at start-up). The Stage keeps working because it talks to the laptop.
3. **You want the writes to show up anyway.** Restart in offline mode: Ctrl+C, then `python -m chief demo --mode demo-offline --no-tunnel`, reopen the Stage, and drive from the laptop. Reads are cached, writes are simulated and tagged. Say it plainly: "I'm replaying recorded results and the changes are simulated; nothing real is happening."
4. **Speech-to-text is down.** Type into the box on the page; it takes the same path as voice.
5. **The tunnel dies but Wi-Fi is fine.** Restart `python -m chief demo` for a new link (the link changes each run) or open the phone page on the laptop.
6. **Nothing works.** `python -m chief demo --mode mock --no-tunnel` runs the full script on seeded data. Say "this is seeded sample data".

Never show the cached/simulated version as if it were live. The tags exist so you do not have to remember.

## What could not be checked without real keys

The replay, failover, doctor and launcher are covered by tests that use fakes. Not verified against the real services: the Composio connection check, the three `--live` calls, a real `record-demo` run (the shape of the live `data` was never seen), cloudflared's real output format (the launcher looks for an `https://*.trycloudflare.com` line), and a real QR package. Do a full dress run once with the real keys.
