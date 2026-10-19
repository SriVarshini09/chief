# Demo script: 2 minutes, one chair

One person in a chair with a phone, a laptop driving a projector. The phone is the voice remote; the **Stage** on the
projector shows everything Chief reads and proposes; the audience watches tasks stack up and nothing change until you say yes.

**Say the phrases exactly as written.** They match Chief's trigger phrases, and they are the same phrases the demo
cache is recorded from (`STORY` in `chief/tools/replay.py`), so if the Wi-Fi dies the answers still come back. Wording drift
is the usual reason a demo "stops working".

> **Be straight about what is running.** Before you start, know which mode you are in and say it in one line. The Stage has a
> mode badge (LIVE, MOCK or CACHED) and tags results that were served from the cache or simulated, but the audience cannot
> read a badge from the back of the room.
> `live`: "This is a throwaway demo Google account with invented people." `demo` or `demo-offline`: "Some of this is replaying
> results I recorded earlier; the Stage marks anything cached." `mock`: "This is seeded sample data."
> The live paths (Composio, Parallel, Slack, phone over a tunnel, an open model) had **not** been run against real services when
> this script was written, so the first rehearsal is a real test, not a formality.

## Setup (once, the evening before)

Details are in the setup guides: [demo-account](demo-account.md), [live-tools](live-tools.md), [phone-setup](phone-setup.md),
[screen-setup](screen-setup.md), [slack-setup](slack-setup.md). How the modes and the Wi-Fi failover behave, and the full
preflight table, are in [demo-day.md](demo-day.md); this page does not repeat them.

1. Seed the demo Google account (dry run first), connect Composio to it, sign the stage browser profile in once
   (`python -m chief show-login`).
2. Record the safety net from the real services: `python -m chief record-demo` (reads only; approvals are never run while recording).
3. `python -m chief doctor` should end with "All critical checks passed". Fix every `[FAIL]`; read every `[WARN]`.
4. Run the whole script below three times: once typed on the laptop, once by voice from the phone, once with the Wi-Fi off
   (`--mode demo-offline`).

## Before you walk on (T minus 10 minutes)

| Check | How |
|---|---|
| Fresh state | `python -m chief reset` (clears decisions and the activity log; this also signs out the default browser profile unless `CHIEF_SHOW_PROFILE_DIR` is set elsewhere) |
| Start | `python -m chief demo` (runs the doctor, serves the page and the Stage, starts the tunnel if `cloudflared` is installed, prints both links and the phrases) |
| Stage | open the Stage link on the projector machine, full screen (F11). Press **R** to wipe it |
| Phone | open the phone link in Chrome, tap **Test voice** (you should hear "Hi, I'm Chief"), do one hold-to-talk test, then press **R** on the Stage again |
| Real windows (live mode, optional) | `--show-windows`; check each opens on the projector screen. If not, close them: the Stage is enough |
| Don't put on screen | the terminal that printed the token link, `.env`, any personal tab |
| Phone audio | the phone is both microphone and speaker. Set media volume low, or untick **Spoken replies** and let the Stage caption talk |

## The script

Times are a target, not a promise: a live model call or search can add seconds. If you are running long at 1:25, drop beat 5.

| Time | You say | What appears on the Stage | Phone | Your next line (aloud) |
|---|---|---|---|---|
| **0:00** | *(no phrase)* | Idle Stage: empty inbox, calendar, research, step log. | Hold to talk button | "This is Chief. It watches my inbox, my calendar and my sleep, tells me what it would do about it, and does nothing until I say yes." |
| **0:10** | **"What's in my inbox?"** | A *heard* row, then *Reading your email*. The inbox panel fills message by message. Caption: the brief. In live mode a Gmail window opens. | Reply text; spoken | "Two to answer, an invoice due the 30th. And one that's just trying to give Chief orders: flagged, not followed." |
| **0:26** | **"Reply to Sam and say Friday works."** | *Saving a draft*; caption: "Saved a draft reply to Sam… Nothing is sent until you approve." | An **Approve / Skip** card for the send | "It wrote the draft from my words. The send is waiting on me. Leave it." |
| **0:42** | **"How's my week looking?"** | *Reading your calendar*, *Reading your sleep data*. The calendar jumps to Thursday. Caption: "You slept under 6 hours after 5 of your last 6 heavy meeting days. Thursday has 6 meetings…" with the action list. | Second card: the audit proposal | "Five of the last six packed days cost me sleep, and Thursday is packed again. It wants to make two calls async, turn one into a walk, and protect a focus block." |
| **1:02** | **"Do it."** | *Approved*, then the writes. Thursday's blocks change to `[Async]`, `Walk:` and `Focus block`, marked UPDATED / NEW. Caption lists what was left waiting: the Sam email. | "Done. 4 change(s) made." plus the reminder that the send is still waiting | "There's the change. And Sam's email is still sitting there. It does exactly what I approved." |
| **1:10** | **"Prep me for my meeting with Dana."** | Reads the calendar, mail threads with Dana, a web search about her company. The research panel shows the query and sources. Caption: the brief. | Reply text | "Past threads and background on Dana's company, with links." *(Optional: "Show me the brief." opens a one-page printable page.)* |
| **1:25** | **"I'm heading to Palo Alto after this, find me a Caltrain after 6 and a boba place on the way."** | Calendar, free time, two searches pinned to caltrain.com / 511.org. Sources appear. Caption: a train time **with its source link**, a boba stop, and an event proposal. | Third card | "It only uses a train time it can point to on the Caltrain page. No source, no time." |
| **1:45** | **"Add it."** | *Approved*; a new event appears on the calendar. | "Done. 1 change(s) made." | |
| **1:52** | *(no phrase)* | Final scene: updated calendar, inbox, sources, full step log. | The Sam card is still pending | "Every write was a yes from me. The Sam reply is still waiting, because I never said yes. It's open source, skills are one file, and it's called Chief." |

Notes on how it behaves, so you are not surprised on stage:

- "Do it" approves the **most recent** pending proposal. In this order that is the audit, which is why the Sam send is still
  waiting afterwards. If you ever want a specific one, tap its **Approve** button on the phone instead of saying "do it".
- Several asks in one sentence run together ("Prep me for Dana and find me a Caltrain after 6 to Palo Alto"); the Stage then shows
  one lane per task. It is a good "tasks stacking" moment, but it is not in the recorded story, so rehearse it before trusting it.
- In **mock** mode the train times and the boba place are labelled MOCK placeholders. Never present those as real.

## Fallback lines

Say these calmly and move on; the audience forgives a recovery and punishes a silence.

| If this happens | Do this | Say |
|---|---|---|
| Voice transcription fails or is wrong | Type the exact phrase in the box on the phone page (or the laptop page) | "The mic is being shy. Same request, typed." |
| The phone loses the tunnel | Open the laptop page (`http://localhost:8765/?token=...`) and type | "Phone's out. The laptop is the same remote." |
| A live call is slow (over ~6 s) | Wait; in `demo` mode a read is answered from the cache after the timeout and the Stage tags it `cached` | "That's a live search. If it's slow, Chief falls back to what I recorded this morning, and it says so." |
| Wi-Fi is gone, you are in `demo` mode | Reads keep working from the cache (tagged). **"Do it" will fail with a clear error: writes are never faked.** Finish the read beats, then switch as in the next row | "The network is down, so Chief fell back to what it recorded, and it labels that. It won't fake a change." |
| You want the writes anyway | Ctrl+C, `python -m chief reset`, then `python -m chief demo --mode demo-offline --no-tunnel`; drive from the laptop page. Reads are cached and writes are simulated and tagged. The reset matters: Chief remembers what you already approved and would otherwise say "nothing worth your attention" | "I'm replaying recorded results and the changes are simulated; nothing real is happening." |
| The Stage freezes or looks stale | Reload it (it re-draws the current scene), or press **R** | "Let me refresh the big screen." |
| Real windows open on the wrong screen or not at all | Ignore them; the Stage shows the same events | Nothing. |
| An approval does not go through | Read the error on the phone. Tap **Approve** again, or **Skip** | "Chief refused. That's the point of the gate: it didn't pretend." |
| Chief says "I can help with: …" | You mis-spoke a trigger. Repeat the phrase exactly | "It wants the exact ask. Again." |
| A model call fails | Nothing: the skills fall back to plain text and the trigger phrases do not need a model | "Plain-text mode. The decisions are code, not a model." |
| Everything is broken | Play the backup video ([video-shotlist](video-shotlist.md)) | "I have a recording of this exact run." |

If a judge asks whether something is real, answer with the table in the README's *Status and verification* section.

## Rehearsal checklist

- [ ] `python -m pytest -q` passes on the demo laptop.
- [ ] `python -m chief doctor` is green; `record-demo` has been run **today** (the doctor warns about a stale cache).
- [ ] Typed run-through of every phrase, in order, in the mode you will use. Each answer matches the table above.
- [ ] Voice run-through from the phone over the tunnel, in the room, at show volume. The transcript of each phrase is exactly right
      (if one is not, retype it in the script to what the transcriber gives you, or pick a phrase it hears reliably).
- [ ] Wi-Fi-off run-through (`demo-offline`).
- [ ] Time it: under 2:00 without hurrying. Cut beat 5 if not.
- [ ] Phone: do-not-disturb on, brightness up, media volume set, screen-timeout long, charger in.
- [ ] Stage: full screen, tested on the real projector at its real resolution; text readable from the back.
- [ ] Nothing personal visible: no real inbox, no `.env`, no token link on a screen the audience can see.
- [ ] `python -m chief reset`, then **R** on the Stage, right before you start.
- [ ] Backup video on the laptop, local file, tested with sound.
- [ ] A one-sentence honest answer ready for "is this live?" (see the box at the top).
- [ ] After the demo: revoke the demo account's app password and the tunnel link; stop `cloudflared`.
