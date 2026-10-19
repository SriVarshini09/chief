# Handoff: Chief (read this first)

Written 2026-10-19 for the next agent. It has no access to the conversation that built this, so everything you need is here or linked.
Where this file and the code disagree, trust the code and fix this file.

## 1. Mission and deadline

Chief is an open-source personal agent built for the **Cline x Composio x Parallel hackathon** (Open Source AI Week, San Francisco,
Monday 2026-10-19, 11:00 AM to 4:30 PM PDT; submissions due 3:15 PM, demos and judging 3:35 PM). The one hard rule is that the project is
open source and shared. Pre-built code is allowed (the owner confirmed with the hosts). Judges are from Cline, Composio and Parallel.

Chief notices things across the user's calendar, email and sleep data, proposes a fix, and **acts only after an explicit approval**.
The user talks to it by voice from a phone, and everything it does appears on a big "Stage" screen, in the style of the GPT-6 Astra
launch video (one person, one chair, tasks stacking, visible work). The target is a live ~2 minute stage demo plus a backup video.

The owner's priorities: a demo that works live, honesty about what is real vs mock, and no personal data on a projector.

## 2. Honest status (what is verified and what is not)

**Verified (on Windows, Python 3.13, mock mode):**
- 636 tests pass, 2 skipped (`.venv\Scripts\python.exe -m pytest -q`). All use fakes; no network.
- The full story runs through the intent layer on seeded mock data and was driven through the real web routes with the Stage page
  open in a browser: inbox, calendar changes with NEW/UPDATED badges, research panel, parallel task lanes, approval cards.
- A manual safety scan found no `shell=True`, `eval`/`exec`, `innerHTML`, or secret printing; subprocess calls take argument lists only.

**NOT verified. Never run against anything real (no keys existed):**
- Composio Calendar and Gmail (`chief/tools/live.py`, `chief/tools/composio_client.py`). The shape of Composio's returned data is
  an assumption (see the docstring at the top of `live.py`); a mismatch shows up as empty lists. This is the biggest risk.
- Parallel web search, Slack Socket Mode (`slack-bolt` is not even installed in the venv), Whisper speech-to-text (Groq),
  the Cline API as the model host, the Cloudflare tunnel, a real phone (Samsung/Chrome), the real-window layer with a real Google
  login, and `seed_demo_account.py --apply` (dry run only).
- Tool calling on any open model. The scripted demo does not need a model; only the open-ended `general` skill and optional phrasing do.
- Nothing has been committed to git before the commit that contains this file.

## 3. Run it (no accounts needed)

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"     # add ".[live]" for real integrations
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m chief run --once --approve   # one heartbeat on seeded data
.venv\Scripts\python.exe -m chief web                    # voice page + Stage; prints URLs with a token
```
Useful for repeatable runs: `CHIEF_NOW=2026-10-21T08:00:00`, `CHIEF_STATE_DIR=<temp dir>`, `CHIEF_WEB_TOKEN=demo123`.
The Stage is at `/stage?token=...`; the phone page is `/?token=...`.

CLI subcommands: `run`, `web`, `skills`, `reset`, `doctor`, `demo`, `record-demo`, `show-login`, `show-demo`, `show-layout`, `slack`.
`python -m chief doctor` prints what is missing (env var names only, never values). `python -m chief demo` is the one-command stage launcher.

Modes (`CHIEF_MODE`): `mock` (seeded data), `live` (real services), `demo` (live, falling back to the recorded cache on failure,
tagged "cached"), `demo-offline` (cached reads, simulated writes tagged "simulated").

## 4. Architecture in one screen

```
speech/typing -> chief/intent.py -> skill(s) -> Proposal -> ApprovalGate -> (approval) -> tools -> Calendar / Gmail / web
                      |                |                                          ^
                conversation.py   ScopedTools (reads only)              RecordingTools -> ActivityBus -> Stage + real windows
heartbeat (chief/heartbeat.py) ticks skills that stay silent unless worth saying (NO_REPLY default)
```
Diagram: `docs/chief-architecture.excalidraw` (regenerate with `scripts/make_diagram.py`).

| Area | Files |
|---|---|
| Core | `chief/runtime.py` (wiring), `gate.py` (approval gate), `memory.py`, `activity.py` (event bus), `heartbeat.py`, `config.py` |
| Routing | `chief/intent.py` (approve/skip words, follow-ups, parallel split, skill triggers), `conversation.py` |
| Skills (one file each) | `audit`, `email_brief`, `draft_reply`, `meeting_prep`, `open_brief`, `trip_planner`, `files`, `general` in `chief/skills/` (contract: `skills/base.py`; guide: `docs/writing-skills.md`) |
| Tools | `tools/base.py` (contract and tool classes), `mock.py`, `live.py` + `composio_client.py`, `recording.py`, `replay.py` (cache and failover), `local.py` (folders/files) |
| Surfaces | `web/app.py` + `index.html` (phone) + `stage.html` (projector), `slack_app.py`, `screen.py` (real browser windows) |
| Demo ops | `launcher.py`, `doctor.py`, `scripts/seed_demo_account.py`, `scripts/tunnel.ps1`, `data/`, `demo/` |
| Docs | `docs/*.md`: `demo-script.md`, `demo-day.md`, `events.md`, `live-tools.md`, `phone-setup.md`, `screen-setup.md`, `slack-setup.md`, `demo-account.md`, `local-actions.md`, `general-assistant.md`, `submission.md`, `video-shotlist.md` |
| Background | `research/chief-research.md` (how Muse, Grok, Dot, OpenClaw, GAIA work; what we copied) |

## 5. Invariants: do not break these

1. **Writes only through `ApprovalGate`.** Skills get `ScopedTools` (read tools only). `WRITE_TOOLS` (`patch_event`, `create_event`,
   `delete_event`, `send_email`) run only after approval. `SAFE_WRITE_TOOLS` (`create_draft`, `post_slack`, `open_folder`, `open_file`) are
   UI-only or harmless. The gate also rejects a skill proposing a tool it did not declare in `propose_tools`.
2. **Approve and skip are plain string matching** in `intent.py` (with filler words stripped, e.g. "Chief, do it please"). A model never
   decides to approve anything.
3. **Email and web text are untrusted.** `draft_reply` builds recipients and subjects from headers and the user's own words, never model
   output; bodies with links or addresses are discarded. The seeded inbox includes an injection email that must stay "flagged, not followed".
4. **Read-only results are never queued** for approval (`gate.submit`), so "do it" cannot land on an empty card.
5. **Honesty about mock/cached/simulated.** Mock mode must never claim something really happened (see `open_brief`/`files`:
   "Mock mode: nothing was really opened"). Cached and simulated results carry flags the Stage shows.
6. **Local actions are allow-listed.** Default allowed folder is only `demo/` (plus `.chief/briefs`). Desktop/Documents/Downloads are opt-in
   through `CHIEF_LOCAL_ROOTS` because real file names would appear on a projector. Executables and scripts are never opened.
7. **The real-window layer only opens or reloads URLs.** It never clicks, types or changes anything in Gmail or Calendar, and it uses a
   dedicated browser profile, never the user's.
8. **Secrets:** never print, log, or commit them. `.env` is git-ignored. Do not ask the user to paste keys into chat.
9. **The model client (`chief/llm.py`) sends `stream=False` and a 20 s timeout.** Cline's API streams by default; without this every
   call silently falls back to canned text.

## 6. What the owner still has to do (an agent cannot)

Creating accounts and entering credentials is the owner's job. `docs/demo-account.md` and the step list below cover it:
1. A **dedicated demo Google account** (address containing `demo`), 2-Step Verification, an app password, IMAP enabled. Values go in `.env`
   (`CHIEF_DEMO_GMAIL`, `CHIEF_DEMO_APP_PASSWORD`). Never use a personal account on stage.
2. **Composio** account and key (`COMPOSIO_API_KEY`, `CHIEF_USER_ID`), then connect Gmail and Calendar *as the demo account*:
   `python -m chief.tools.composio_client gmail|calendar|--status`.
3. **Parallel** key (`PARALLEL_API_KEY`), **Groq** key for Whisper (`CHIEF_STT_API_KEY`), and a model key. The owner plans to use the
   **Cline API**: `CHIEF_LLM_BASE_URL=https://api.cline.bot/api/v1`, a `provider/model` ID such as `deepseek/deepseek-chat`, key from app.cline.bot.
4. `winget install --id Cloudflare.cloudflared` (the phone mic needs HTTPS), then `python -m chief show-login` to sign the demo account
   into Chief's separate browser profile.

## 7. Recommended next steps, in order

1. `pip install -e ".[live]"`, then `python -m chief doctor`. Fix what it reports.
2. **First real run** of Calendar and Gmail through Composio (a small script that calls `LiveTools.list_events` and `fetch_emails`).
   Compare the real response shapes to the assumptions in `live.py` and fix the parsers. Check how Composio wants time strings in create/patch.
3. Run `scripts/seed_demo_account.py` as a dry run, then `--apply --i-am-using-a-demo-account` once the owner approves.
4. Run `python -m chief record-demo` to fill `data/demo_cache.json` (the stage safety net; git-ignored because it holds test-account snippets).
5. Test a **real phone over the tunnel** (mic permission, Whisper route, spoken replies). Chrome on Android lists browser speech recognition as
   "partial"; the server-side Whisper path is the primary one.
6. Benchmark tool calling on the Cline models (`deepseek/deepseek-chat`, `minimax/minimax-m2.5`) before trusting the `general` skill live.
7. Rehearse `docs/demo-script.md` end to end, then record the backup video (`docs/video-shotlist.md`).
8. Keep `README.md` "Status and verification" honest as things get verified.

Known small issues: the Stage calendar header labels can overlap at narrow widths; the `/ask` JSON response does not include the per-task
list (the Stage gets task lanes from activity events instead); Slack is untested and optional; the DevTools window-reload path in `screen.py` is
unverified with a real browser.

## 8. Gotchas for working here

- **Windows:** very long paths break some tools (PowerShell `Test-Path` returned false for a path over 260 characters). Use short temp paths.
- Tests must stay green and offline. Never add a test that opens a real file, window, or network connection.
- Several people (or agents) may edit at once: re-read a file right before editing it and keep edits small.
- The mock tools are deterministic relative to `now` (heavy meetings Mon/Tue/Thu); tests pin `NOW = 2026-10-21 08:00` (a Wednesday).
- Test fixtures contain deliberately fake key-like strings (`tests/test_doctor.py`, `tests/test_replay.py`) that verify redaction. They are not real.
- The git history is a single commit by design. The machine this was built on is logged into GitHub as a different account
  than the repo owner (`premxai`); check `gh auth status` before pushing and never force-push.

## 9. If you only have 20 minutes

Run the tests, run `python -m chief web`, open `/stage`, type "how's my week looking?", then "do it", then "what's in my inbox?". Read
`docs/demo-script.md`. Then do step 1 and 2 of section 7, because the first real Composio call is where the remaining risk is.
