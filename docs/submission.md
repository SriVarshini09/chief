# Submission materials

Text for the hackathon submission form, plus what a judge can check in the repository and where the project falls short.
Everything here is consistent with the README's *Status and verification* section; if the two ever disagree, the README
table (updated from what was actually run) wins.

Word counts: the short description is exactly 100 words and the long one is 400 words (counted by script, headings excluded).

## 100-word description

Chief is an open-source personal agent that notices things in your calendar, email and sleep data, proposes a specific fix, and acts only after you approve. Talk to it from your phone; a Stage screen shows every read, search and proposal as it happens. Every write waits behind an approval gate, tools are scoped per skill, and email and web text is untrusted. Skills are single files of about twenty lines. Built on Composio, Parallel and any OpenAI-compatible open model. It runs end to end on seeded data; live service paths are written but not yet run against real accounts.

## 400-word description

Most assistants wait to be asked, or act without asking. Chief does neither. It runs a quiet heartbeat over your calendar, email and sleep data and says nothing unless something deserves your attention. When it does, it proposes a specific fix, and nothing changes until you tap Approve.

In the demo, one person sits in a chair and talks to a phone. "What's in my inbox?" fills a big Stage screen with the messages Chief is reading and flags the one trying to give it orders. "Reply to Sam and say Friday works" saves a draft; the send waits. "How's my week looking?" compares sleep with meeting load, notices that packed days have cost several short nights, and proposes marking two meetings async, making one a walking call and protecting a focus block. "Do it" applies exactly that, and the Stage shows the calendar change. Asking for a Caltrain after six returns a time only if it appears on a pinned source page, with the link; otherwise Chief says it cannot find one.

Safety does not rely on the model behaving. A skill gets only the read tools it declared; a write can only be proposed, and the approval gate is the single place one runs. The gate independently rejects a proposal containing a tool the skill never declared. Email, calendar and web text is handled as untrusted data, and instruction-like mail is flagged and ignored. Approval is plain string matching, never a model's decision.

Chief is built on the sponsor tools. Composio supplies Google Calendar and Gmail through separate read and write sessions. Parallel Search supplies web results for trips and meeting background, pinned to trusted domains. Language and speech run on any OpenAI-compatible open model host. Each skill is one file of about twenty lines, and tests enforce the safety rules, so a coding agent such as Cline can add one and the test run shows whether it kept them.

Everything runs end to end in mock mode on seeded data without accounts, and the tests pass. We are explicit about what has not happened: the live Composio, Parallel, Slack, Whisper and phone-over-tunnel paths are written and unit-tested against fakes but have not been run against real services, and no open model has been benchmarked. The repository's Status and verification section says so. The demo has a recorded-results failover that never fakes a write. It is MIT licensed.

## What is novel

Stated narrowly; none of these is a claim of "first".

- **Proposals, not actions, are the unit of work.** A skill returns a `Proposal`; a human approval, matched by plain string comparison or a button, is the only way a write runs. The model never approves anything.
- **Two independent safety layers, both tested.** `ScopedTools` hides write methods and undeclared tools from a running skill; the approval gate separately rejects a proposal that includes a tool the skill did not declare. The same rule is applied to every skill by a loop in the contract tests.
- **Silence as the default.** The heartbeat skill returns `None` unless something deserves attention, and a decided situation is never proposed twice.
- **One event stream drives every screen.** Every read, write and approval is an event on the `ActivityBus`; the Stage page and the real browser windows are just subscribers, so the projector shows what Chief is actually doing rather than a scripted animation.
- **Failover that does not lie.** In demo mode, reads fall back to recorded results, writes are never faked (a failed write raises), and cached or simulated results are flagged on the Stage.
- **It will not invent a train time.** A time is used only if it appears in a search result from a pinned source, with the link attached; if not, Chief says so and proposes nothing.
- **A prompt-injection test is part of the story.** The seeded inbox contains an email that tells an assistant to forward the mailbox; the demo shows it flagged and ignored.
- **Skills are one file and the rules are executable.** Dropping a file into `chief/skills/` registers it; tests fail a skill that writes directly or proposes undeclared tools.
- **Voice-first from a phone** over a tunnel, with a typed fallback that shares the same intent path, plus several asks in one sentence running as parallel tasks with one lane each on the Stage (not part of the recorded story; rehearse it before relying on it).

## What judges can verify in the repo

No accounts or keys are needed for anything in this list.

| To check | Do this |
|---|---|
| It runs and the safety rules hold | `.venv\Scripts\python.exe -m pytest -q`. Read `tests/test_safety.py` (no write before approval; undeclared tools rejected; skip is remembered; a failed action is not recorded as done) and `tests/test_skills_contract.py` |
| The demo story works in mock mode, in order | `tests/test_story_e2e.py` plays the exact phrases from the demo script |
| The loop and the gate | `python -m chief run --once` (proposes, changes nothing) then `python -m chief run --once --approve` |
| Where a write can happen | `chief/gate.py` (about 110 lines) and `ScopedTools` in `chief/tools/base.py`. Search the repo for `WRITE_TOOLS` |
| Untrusted text handling | `chief/skills/email_brief.py` (detection and flagging), `chief/skills/draft_reply.py` (recipient from headers, no links in bodies); the seeded injection email is `m8` in `chief/tools/mock.py` |
| The "no invented train time" rule | `chief/skills/trip_planner.py` and `tests/test_skills_trip.py` |
| Local actions are allow-listed | `chief/tools/local.py` and `tests/test_local_tools.py` |
| Demo failover never fakes a write | `chief/tools/replay.py` and `tests/test_replay.py` |
| What the live code calls, and what is unverified | [live-tools.md](live-tools.md): every Composio slug and argument, with a table of what was and was not checked |
| A skill is one file | [writing-skills.md](writing-skills.md), then add the worked example and run `python -m chief skills` |
| The architecture | `docs/chief-architecture.excalidraw` (open in excalidraw.com) or the preview image; regenerate with `python scripts/make_diagram.py` |
| The demo and the video plan | [demo-script.md](demo-script.md), [video-shotlist.md](video-shotlist.md) |

What a judge **cannot** verify from the repo alone is whether the live paths work, because that needs keys and accounts.
The first full run on the demo account is the real test of those paths.

## Honest limitations

- **The live paths have not been run against real services.** Composio Calendar and Gmail, Parallel search, Slack Socket Mode,
  Whisper speech-to-text and the phone-over-tunnel path are written, reviewed against SDK source and docs, and unit-tested with fakes.
  Field names in Composio's responses, time-zone handling and several argument behaviours are marked unverified in [live-tools.md](live-tools.md).
- **No open-source model has been called or benchmarked.** In mock mode the model is disabled and all text is deterministic.
  How well a given open model handles tool calling in the `general` skill is unknown; the benchmark is in [general-assistant.md](general-assistant.md).
- **Cline is not integrated.** Nothing in Chief calls Cline. The skill format is designed so a coding agent can extend it, but that
  workflow has not been tried with Cline.
- **Mock data is generated.** The calendar, mail, sleep and train data in mock mode are invented samples; the train times and boba place
  there are labelled MOCK placeholders. Sleep data is a generated sample, not real health data; there is no real health import.
- **The prompt-injection defences are a speed bump.** Instruction detection is a list of cues and the untrusted-data tags rely on the
  model obeying the system prompt. The actual protection is that nothing can write without approval. A write that you approve is only
  as good as the proposal text you read, and Chief does not yet show a diff of exactly what an email will say beyond the card.
- **Approval is coarse.** "Do it" approves the most recent pending proposal; Slack buttons can be pressed by anyone who can see the channel;
  the phone page's only protection is a secret token in the URL.
- **One user, one machine.** State is local files; there is no multi-user model, no authentication beyond the token, and the heartbeat is a
  loop in one process.
- **Real windows are a presentation layer, not verified on real Google pages.** No real sign-in or real Gmail/Calendar page has been driven.
- **Speech.** Push-to-talk through a recorded clip, not streaming. Spoken replies use the browser's built-in voice and read the whole reply.
- **Demo mode is a safety net, not a feature of the product.** Cached results are only as fresh as the last recording; the Stage flags them.
- **Not hardened.** No rate limiting, no audit of dependencies, no security review beyond the tests listed above.
