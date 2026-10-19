# Talking to Chief from your phone

Chief's voice page is a single web page served from your laptop. You open it in Chrome on the phone, hold the big button, speak, and Chief answers out loud. Approve or Skip buttons appear for anything Chief wants to do.

Phones only allow microphone access on **HTTPS** pages (or `localhost`). A plain `http://192.168.x.x:8765` link over Wi-Fi will load but the mic will be blocked, so we use a Cloudflare quick tunnel, which gives the laptop a public HTTPS address.

## 1. One-time setup

```powershell
# the web server's dependencies
.venv\Scripts\python.exe -m pip install -e ".[live]"

# the tunnel client (not bundled, not installed by Chief)
winget install --id Cloudflare.cloudflared
```

Open a new terminal after the winget install so `cloudflared` is on `PATH`.

Add these to your `.env` (never commit it):

```
CHIEF_WEB_TOKEN=pick-a-long-random-string
CHIEF_STT_API_KEY=<your Groq key>        # speech-to-text for the push-to-talk button
# optional, these are the defaults:
# CHIEF_STT_BASE_URL=https://api.groq.com/openai/v1
# CHIEF_STT_MODEL=whisper-large-v3-turbo
```

`CHIEF_STT_*` can point at any OpenAI-compatible Whisper endpoint. Without a key the page still works by typing.

`CHIEF_WEB_TOKEN` guards every route. If you leave it unset, `python -m chief web` makes a random one each run and prints it.

## 2. Start it

Terminal 1, the page:

```powershell
.venv\Scripts\python.exe -m chief web --port 8765
```

It prints `http://localhost:8765/?token=...`. Open that on the laptop first; you can already type to Chief there.

Terminal 2, the tunnel:

```powershell
.\scripts\tunnel.ps1 -Port 8765
```

`cloudflared` prints a line like `https://random-words.trycloudflare.com`. On the phone, open:

```
https://random-words.trycloudflare.com/?token=<your CHIEF_WEB_TOKEN>
```

The address changes every time you restart the tunnel, so do this shortly before you need it. Anyone holding the full link can reach the page and approve actions, so treat it like a password.

## 3. Test the microphone on the phone

1. Open the link in **Chrome for Android**. Do the first test somewhere you can retry; don't leave it for the stage.
2. Tap **Test voice**. You should hear "Hi, I'm Chief." If not, check the media volume and that "Spoken replies" is ticked. Chrome on Android only speaks after you've touched the page, so tap the button once even if you plan to type.
3. Press and hold **Hold to talk**. Chrome asks for microphone permission the first time; tap **Allow**. (A quick tap instead of a hold latches recording on until you tap again, which is easier with a shaky hand.)
4. Say "How's my week looking?" and let go. The button shows "Transcribing…", then Chief answers in the panel and out loud, and an Approve/Skip card appears.
5. Say "Do it" (or tap **Approve**) to apply it.

If something fails:

| Symptom | Fix |
| --- | --- |
| "This page needs HTTPS to use the microphone" | You opened the plain `http://` address. Use the `https://...trycloudflare.com` one. |
| "Microphone blocked" | Chrome menu, Settings, Site settings, Microphone: allow the page, then reload. |
| "Speech-to-text is not configured" | `CHIEF_STT_API_KEY` is missing in the server's environment. Restart `python -m chief web` after editing `.env`. |
| "Speech-to-text failed" | Check the key and network. Typing in the box uses the same path as voice, so you can carry on. |
| Spoken reply is silent | Tap **Test voice** once, check media volume. Android may also need a TTS engine (Settings, Text-to-speech). |
| "Wrong or missing token" | The link needs `?token=` with the exact `CHIEF_WEB_TOKEN` value. |

The **Browser speech recognition** checkbox is an optional shortcut that skips the server round trip. Chrome on Android lists it as partial support, so it is off by default; the server-side Whisper path is the one to rely on.

## The Stage: a big screen that shows everything Chief does

The phone page is the remote control. The **Stage** is what goes on the projector: ask for a brief and the inbox fills in, ask about your week and the calendar changes, ask for research and the searches and sources appear, all while a step log lists every event in order.

On the laptop that drives the projector, open (same token as the phone):

```
http://localhost:8765/stage?token=<your CHIEF_WEB_TOKEN>
```

Put the browser in full screen (F11). The Stage has no controls: it just follows Chief.

| Panel | What appears |
| --- | --- |
| Inbox | each message as Chief reads it: sender, subject, snippet |
| Calendar | a day view, 8:00 to 18:00, for the next day with events plus the day before. Overlapping events sit side by side and the type shrinks before any title is cut off; `[Async]` blocks are striped. See "Which day the calendar shows" below. After you approve, renamed blocks (`[Async]`, `Walk:`) and new ones (`Focus block`) are marked UPDATED / NEW |
| Research | each web search as it runs, then its sources: title, URL, first excerpt |
| Step log | heard, step, working, read, proposal, approved, say. A "working" row turns into its result when the result arrives |
| Caption | what Chief says, in large type, plus one approval card for each proposal waiting, with its action list |
| Mode badge | top right: `LIVE`, `MOCK` or `CACHED` (from `CHIEF_MODE`: live, mock, demo) |

**Which day the calendar shows.** Until something names a day it shows the next day with events and the day before. When Chief proposes a change, the Stage moves to the day it would change (so the audience sees the "before") and stays there after you approve. While one proposal is waiting, a newer proposal for another day does not pull the screen away; when the older one is approved or skipped the screen follows the next waiting one. Press **R** to go back to the default.

**Parallel tasks.** If events carry `detail.task` (a short id), the Step log, Inbox and Research panels split into one lane per task, colour-coded, each with its own "working..." state. Without that field everything looks exactly as before, in a single lane.

**Honesty tags.** An event whose `detail` has `cached: true` or `simulated: true` gets a small `cached` / `simulated` tag in the step log and on the item and panel it feeds (the panel tag stays until you press R, so a screen mixing real and cached data says so).

Try it in mock mode (`python -m chief web`): open `/stage` on one screen and the phone page on another, then say or type "how's my week looking?", "do it", "what's in my inbox?".

Notes:

- Press **R** on the Stage to wipe it between run-throughs. Opening or reloading it mid-demo shows the current scene straight away (from the last thing you said), not the whole history.
- It polls `/stage/events` twice a second, so it works through the tunnel. Events are revealed at most about a quarter of a second apart so a fast mock run still reads as work; a long backlog is drained within a few seconds, so the screen never falls behind.
- Open the Stage through the tunnel link too if the projector is a different machine. The token is the same: anyone with the link can watch what Chief reads (email subjects and snippets, calendar), so don't share it.

## What the page talks to

| Route | Purpose |
| --- | --- |
| `GET /` | the page |
| `POST /stt` | recorded audio in, transcript out (server calls the Whisper endpoint) |
| `POST /ask` | `{"text": "..."}` in, `{"say", "proposal", "steps"}` out |
| `POST /approve/{id}`, `POST /skip/{id}` | resolve a pending proposal through the approval gate |
| `GET /status` | polled once a second: pending proposals, progress steps, last reply |
| `GET /stage` | the Stage page (big screen) |
| `GET /stage/events?since=SEQ` | activity events after `SEQ`, polled twice a second by the Stage |
| `GET /stage/calendar` | the day view's data; `?day=YYYY-MM-DD` or `?event=ID` pins the day (`pinned` in the reply says whether the event or day was found). Reads without adding to the activity stream |

Progress is polled, not streamed, because Cloudflare quick tunnels don't support Server-Sent Events.
