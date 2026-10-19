# Backup / submission video: shot list

A 2:20 video that works as the backup if the live demo fails and as the submission video. It borrows the shape of a
"talk to your computer" launch film: a short hook, then **one continuous take** of one person in one chair talking to a
phone while a big screen shows the work. No cuts during the demo, so the audience can see that nothing is edited.

Target length 2:20 (hook 0:12, title 0:03, take 1:55, end card 0:10). Captions throughout; the video must make sense with the
sound off.

## Ground rules (so the video is honest)

- Record the take in the mode you will describe. If it is `demo` / `demo-offline` (cached reads, simulated writes), say so
  in the end-card caption. If it is `live` on the throwaway demo account, say that. If it is `mock`, say "seeded sample data".
  The live paths had not been run against real services when this was written; do not caption a take as "live" unless it was.
- The people, companies and addresses in the data are invented. Do not show a real inbox.
- The take must be the real script ([demo-script.md](demo-script.md)), unedited inside the take. Trim only the start and end.
- Mark a recovered mistake rather than hiding it: a re-take is fine, a spliced take is not.

## Equipment and set

| Item | Notes |
|---|---|
| Camera | one fixed wide shot: the person in the chair on the left, the Stage screen (projector or a large monitor) readable on the right. 4K if possible so the text is crisp |
| Screen capture | record the Stage machine's screen at the same time as a clean second angle (not cut in; kept as a backup and for stills) |
| Audio | lavalier or close mic on the speaker. Turn **Spoken replies** off on the phone (the phone mic and speaker are the same device) and caption Chief's replies instead; or keep them on and place the phone away from the lav and record it as a separate track |
| Set | plain wall, desk lamp, a chair. Nothing personal in frame. Phone held in hand, thumb on the talk button |
| Mode | `python -m chief demo` (see demo-script setup). `python -m chief reset`, then **R** on the Stage immediately before recording |
| Captions | burn in white text on a dark bar at the bottom, one line at a time, 3 to 5 seconds each |

## Shot list

| # | Time | Shot | Action and audio | Caption |
|---|---|---|---|---|
| 1 | 0:00 to 0:12 | **Hook.** Warm, slightly dated office. An executive in a chair, a human assistant standing with a notepad | Executive: "Check my inbox. Reply to Sam. And how does my week look?" The assistant writes it down, leaves, comes back with a stack of paper, flips to a page, answers. The executive nods and signs a sheet. A wall calendar is crossed out by hand | "An assistant. 1996." then "It took an afternoon, and someone had to be paid." |
| 2 | 0:12 to 0:15 | **Title.** Hard cut to black, one clean card | Silence, a single click sound | "2026" |
| 3 | 0:15 to 0:20 | **Wide shot, same chair, same person.** Stage on the right, empty. Phone in hand | Person (to camera, calm): "This is Chief. It watches my inbox, my calendar and my sleep. It tells me what it would do, and it does nothing until I say yes." | "Chief: an open-source personal agent." |
| 4 | 0:20 to 0:36 | Take continues. Person raises the phone | Say: "What's in my inbox?" Stage: inbox fills, caption appears. Say over it: "Two to answer, an invoice due, and one message trying to give Chief orders. It flagged it and ignored it." | "1. Reads your inbox. Treats email as data, not orders." |
| 5 | 0:36 to 0:50 | Same take | Say: "Reply to Sam and say Friday works." Stage: draft saved. Say: "The draft is made. Sending waits for me." | "2. Drafts the reply. Nothing is sent." |
| 6 | 0:50 to 1:10 | Same take. Hold on the Stage calendar jumping to Thursday | Say: "How's my week looking?" Stage: calendar and sleep read, Thursday opens, proposal text. Say: "Five of my last six packed days cost me sleep, and Thursday is packed again. It wants to make two calls async, turn one into a walk, and protect a focus block." | "3. Notices the pattern. Proposes a fix." |
| 7 | 1:10 to 1:18 | Same take | Say: "Do it." Stage: blocks change to `[Async]`, `Walk:`, `Focus block`. Say: "And Sam's email is still waiting, because I haven't said yes to that one." | "4. Acts only after approval." |
| 8 | 1:18 to 1:33 | Same take | Say: "Prep me for my meeting with Dana." Stage: threads and sources fill in. Say: "Past threads and background, with links." | "5. Preps your meetings. Cites sources." |
| 9 | 1:33 to 1:52 | Same take. Hold on the research panel with the source link visible | Say: "I'm heading to Palo Alto after this, find me a Caltrain after 6 and a boba place on the way." Stage: searches and sources. Say: "It only uses a train time it can link to on the Caltrain page." Then: "Add it." New calendar event appears | "6. Won't invent a train time. Shows its source." |
| 10 | 1:52 to 2:08 | Same take. Person turns to camera, phone down. Stage shows the full step log | Say: "Everything it did is on that screen. Every change was a yes from me. Skills are one file, about twenty lines. It's open source, and it's called Chief." | "Every read, write and approval is on screen." |
| 11 | 2:08 to 2:20 | **End card.** Cut to a plain card | Silence or a soft tone | Line 1: the repo URL. Line 2: "Open source (MIT). Skills are one file." Line 3 (required): what the take ran on, e.g. "Recorded on a throwaway demo account with invented data" or "Replayed recorded results; writes simulated". Line 4: "Live Composio / Parallel / Slack / open-model paths: see the repo's Status and verification section" |

Times in rows 4 to 10 are targets for one continuous take of about 1:55; the real take will drift by a few seconds. Re-time the
captions to the footage, not the other way round.

## Captions to burn in (in order)

1. An assistant. 1996.
2. It took an afternoon, and someone had to be paid.
3. 2026
4. Chief: an open-source personal agent.
5. 1. Reads your inbox. Treats email as data, not orders.
6. 2. Drafts the reply. Nothing is sent.
7. 3. Notices the pattern. Proposes a fix.
8. 4. Acts only after approval.
9. 5. Preps your meetings. Cites sources.
10. 6. Won't invent a train time. Shows its source.
11. Every read, write and approval is on screen.
12. (end card, honesty line: what the take ran on)

## If the take goes wrong

- A wrong transcript: retype the phrase in the box on camera ("typed, same request") rather than cutting.
- The Stage lags: pause one beat and keep talking; do not cut away.
- Spoken replies bleed into the mic: turn them off and re-take.
- Three bad takes: record the Stage screen capture with a voice-over of the same script as the fallback cut, and caption it
  "voice-over of a real run".

## Before you export

- [ ] Watch it once with the sound off: do the captions carry the story?
- [ ] No token link, `.env`, personal tab or real inbox visible in any frame, including reflections and the phone screen.
- [ ] The end card states what the take ran on, truthfully.
- [ ] Check the submission form's file-size limit, or host the video unlisted and link it; keep the raw take.
- [ ] Alt text / description: link to the repo and to the README's *Status and verification* section.
