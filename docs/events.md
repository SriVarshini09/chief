# Activity events and `handle_utterance` (parallel tasks and follow-ups)

Everything Chief does is an `Activity` on `rt.bus` (`chief/activity.py`). The Stage polls it. This page
lists what was **added** for parallel requests and follow-ups; nothing existing was renamed or removed.

## Event fields

`Activity.to_json()` is unchanged: `{seq, at, kind, title, tool, detail, ok}`.

### `detail["task"]`

While a task runs, every event emitted on its behalf has `detail["task"] = "t3"` (a short id, unique for
the life of the runtime, `t1`, `t2`, ...). This covers `step`, `working`, `read`, `write`, `proposal`,
`error`, and also the `approved` / `skipped` / `write` events when you later approve or skip the proposal
that task made (they land in the lane that proposed them). Events with no task are the utterance-level
ones: `heard`, `say`, and the plan event below. Draw one lane per distinct `detail["task"]`.

### `step` events

| title | detail | meaning |
|---|---|---|
| `Running 2 tasks at once` | `{"plan": [{"task", "skill", "request"}]}` (no `task`) | an utterance was split; create the lanes now |
| `Running <skill>` | `{"skill", "request", "task"}` | a task started (already existed) |
| `<skill>: finished` | `{"skill", "state": "done", "proposal_id", "read_only", "task"}` | a task ended; `proposal_id` is null if it had nothing to say |

A single-task utterance also gets a task id (just one lane).

### `say` event

`detail` keeps `steps` and `proposal_id`, and adds:

- `proposal_ids`: ids of every proposal made for this utterance, in the order spoken
- `tasks`: the same list as `handle_utterance(...)["tasks"]`

### `skipped` with `detail["superseded_by"]`

A follow-up edit ("make that shorter") replaces a pending proposal with a NEW one. The old card gets a
`skipped` event whose `detail` has `superseded_by` = the new proposal id; the new one arrives as a normal
`proposal` event. The old one is not recorded as rejected.

## `handle_utterance(rt, text)` return value

```python
{
  "say": str,                 # what to read out; for several tasks it lists them and what is pending
  "proposal": Proposal | None,  # unchanged: for several tasks, the latest one that has something to approve
  "steps": [str],             # unchanged
  "proposals": [Proposal],    # NEW: every proposal made for this utterance, in the order spoken
  "tasks": [{"id", "skill", "say", "proposal_id"}],  # NEW: one per task; [] for approve/skip/follow-up edits
}
```

`proposals` includes read-only results (no actions). Only proposals with actions are pending.

## Words

| you say | effect |
|---|---|
| `do it`, `yes`, `approve` | approves only the most recent pending proposal (the last one spoken) |
| `approve all`, `do both`, `do all of it` | approves every pending proposal from Chief's last answer |
| `skip`, `never mind`, `undo that` | drops the most recent pending proposal (nothing else; finished work is never undone) |
| `skip all`, `cancel both` | drops every pending proposal from the last answer |
| `make that shorter` / `more formal` / `friendlier` | rewrites the pending draft's body: a new gated proposal |
| `make the vendor sync a walk` | turns that calendar change into a walk: a new gated proposal |
| `and what about tomorrow?` | re-runs the last skill for another day (trip planner: today/tomorrow; general skill: any day) |

Replies that leave things pending say so plainly ("Waiting for your OK on 2 things: ... 'do it' approves
only the latest (2). 'approve all' approves the 2 from my last answer.").

## Safety notes

- Splitting needs each side to match a different skill's trigger phrase. With a model, the split is only
  tried when no trigger matched, and each piece must be a literal slice of what you said.
- A follow-up edit never writes: it builds a new `Proposal`, which goes through `gate.submit` again
  (declared-tools check, card, `proposal` event). `gate.supersede` refuses edits the skill could not
  have proposed and keeps the original in that case.
- Short-term context (`rt.conversation`, last 6 turns) is only handed to the `general` skill, fenced as
  data, never as instructions.
