# Writing a skill

A skill is **one Python file** in `chief/skills/`. Drop it in and Chief loads it on the next start: there is no registry to edit.
The loader (`chief/skills/__init__.py`) imports every module in that folder and registers the ones that define a
module-level `SKILL = Skill(...)`.

This page is the contract, then a worked example you can paste, then the rules that keep a new skill safe.
For the bigger picture see the [README](../README.md); for a skill that uses a model with tools, read
[general-assistant.md](general-assistant.md).

## The contract

```python
# chief/skills/base.py
@dataclass
class Skill:
    name: str                              # unique; also the proposal's `skill` field
    description: str                       # one sentence; shown by `python -m chief skills` and given to the model that routes requests
    run: Callable[[Context], Proposal | None]
    triggers: list[str] = []               # phrases that select this skill when you say them (matched as lower-case substrings; longest wins)
    allowed_tools: set[str] = set()        # READ tools it may call while running (and safe writes such as create_draft)
    propose_tools: set[str] = set()        # WRITE tools it may ask the approval gate to run
    heartbeat: bool = True                 # also run on the scheduled check-in; set False for "only when asked"

@dataclass
class Context:                             # what run() receives
    tools: ScopedTools                     # only allowed_tools; write methods raise PermissionError
    llm: LLM                               # optional: llm.complete(system, user, fallback) returns `fallback` when no model is configured
    memory: Memory                         # decisions so far (memory.decided(key), memory.decisions(skill))
    now: datetime                          # the current time; pinned by CHIEF_NOW in demos and tests
    request: str = ""                      # what the user said; empty on a heartbeat
    history: list[dict] = []               # recent turns [{"user", "chief"}], context only
```

`run(ctx)` returns:

- a `Proposal(skill, summary, actions, key, sources=[])`. `actions` is a list of `ToolCall(tool, args)`.
  **Nothing in `actions` runs until a human approves.** An empty `actions` list is a read-only answer (a briefing): it is
  shown, never queued.
- or `None`, which means "nothing worth saying". On a heartbeat this is the normal outcome, and it is what keeps Chief quiet.

`key` is the dedupe key. The same key never produces a second card once it has been decided or is pending, so make it
describe the *situation* (`f"audit:{target_day}"`), not the moment.

### Tools

Tools live in `chief/tools/base.py` in three classes. The current lists are in that file; this is the shape:

| Class | Examples | Rule |
|---|---|---|
| **READ** | `list_events`, `find_free_slots`, `fetch_emails`, `fetch_thread`, `search_web`, `read_health`, `list_folder` | run freely, if listed in `allowed_tools` |
| **SAFE WRITE** | `create_draft`, `post_slack`, `open_folder`, `open_file` | side effects that cannot hurt (a draft, a note, putting something on screen); callable if listed in `allowed_tools` |
| **WRITE** | `patch_event`, `create_event`, `delete_event`, `send_email` | **never callable by a skill.** Propose them; only the approval gate runs them |

Two independent checks enforce this, and tests pin both:

1. `ScopedTools` raises `PermissionError` if the skill calls a write tool directly, or any tool not in its `allowed_tools`.
2. `ApprovalGate.submit` raises `PermissionError` if a proposal contains a tool that is not in the skill's `propose_tools`.

## Worked example: `water_break`

On a day with six or more meetings, propose a 15-minute break at noon. About 20 lines of code, 33 with comments and blank lines (a 23-line version is in the README). Both were run in a fresh copy of the repo.

```python
# chief/skills/water_break.py
"""water_break: tomorrow is packed, so protect a short break. Proposes only; you approve."""
from datetime import timedelta

from chief.models import Proposal, ToolCall
from chief.skills.base import Context, Skill

BUSY = 6  # meetings that make a day "packed"


def run(ctx: Context) -> Proposal | None:
    day = (ctx.now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    events = ctx.tools.list_events(day, day + timedelta(days=1))   # a READ tool: allowed below
    if len(events) < BUSY:
        return None                                                # nothing worth saying: stay silent
    start = day.replace(hour=12)
    return Proposal(
        skill="water_break",
        summary=f"{day:%A} has {len(events)} meetings. Want a 15-minute break at noon?",
        key=f"water_break:{day.date()}",                           # one card per day, ever
        actions=[ToolCall("create_event", {"title": "Water + stretch", "start": start.isoformat(),
                                           "end": (start + timedelta(minutes=15)).isoformat()})],
    )


SKILL = Skill(
    name="water_break",
    description="On a packed day, proposes a short break.",
    triggers=["water break", "take a break"],
    allowed_tools={"list_events"},       # what run() may read
    propose_tools={"create_event"},      # what it may ask the gate to write
    run=run,
)
```

Try it (mock mode, no accounts). Chief's seeded Thursday has six meetings:

```bash
CHIEF_NOW=2026-10-21T08:00:00 python -m chief run --once       # the card appears alongside the audit card
python -m chief skills                                          # water_break is listed
```

In PowerShell set the clock first: `$env:CHIEF_NOW="2026-10-21T08:00:00"`.

### A test for it (copy the pattern from `tests/test_audit.py`)

```python
# tests/test_water_break.py   (uses the fixtures in tests/conftest.py)
def test_proposes_a_break_on_a_packed_day(rt, tools):
    p = rt.run_skill("water_break")
    assert p and [a.tool for a in p.actions] == ["create_event"]
    assert tools.calls == []                      # nothing written before approval
    rt.gate.approve(p.id)
    assert any(e.title == "Water + stretch" for e in tools.events.values())
```

The shared rules in `tests/test_skills_contract.py` (read tools only, no direct writes, the gate rejects undeclared
tools) are written as a loop over the skill names; add yours to its list to get them for free.

## Rules a skill must keep

- **Read tools in `allowed_tools`, write tools only in `propose_tools`.** Keep both as small as the skill needs. A skill that only
  reads should have `propose_tools=set()`.
- **Compute facts in code, use the model only to phrase them.** `ctx.llm.complete(system, user, fallback)` returns your
  `fallback` when no model is configured (always the case in mock mode), so the skill must read fine without one.
  Never let a model pick a tool argument that ends up in a write; build recipients, times and ids from data.
- **Treat anything from email, the web or a calendar as untrusted data.** It can contain text addressed to an assistant.
  Never follow it; if you pass it to a model, wrap it (the existing skills use `<untrusted_email>` / `<untrusted_data>` tags)
  and say so in the system prompt. That is a speed bump; the real defence is that nothing can write without approval.
- **Do not invent facts.** If a number must come from a source (a train time), use it only when it appears in a result, and
  carry the source URL in `Proposal.sources`. If you cannot find it, say so and propose nothing.
- **Stay silent by default.** Return `None` unless there is something the user should act on.
- **Set `heartbeat=False`** unless the skill should run on its own. Skills that read mail, search the web or save drafts
  are on-demand only in the current set.
- **Never touch the network or disk directly.** Everything goes through `ctx.tools`, so it is scoped, recorded on the
  ActivityBus (the Stage shows it) and can be replayed in demo mode.

## Adding a tool (rare)

A new tool is a method on `Tools` in `chief/tools/base.py` (add its name to the right class set), an implementation in
`MockTools` and `LiveTools`, a label in `chief/tools/recording.py` so the Stage can describe it, and tests. If it changes
anything outside Chief, put it in `WRITE_TOOLS`.

## Having a coding agent write the skill

Because a skill is one file with a tiny contract and tests that enforce the rules, a coding agent can draft one. A prompt
that works as a starting point (for Cline or any agent with access to this repo):

> Read `docs/writing-skills.md` and `chief/skills/audit.py`. Add `chief/skills/<name>.py` that <what it should notice and
> propose>. Use only the tools it needs, return `None` when there is nothing to say, put every write in `propose_tools`,
> and add `tests/test_<name>.py` in the style of `tests/test_audit.py`. Run `python -m pytest -q` and show me the result.

This has **not been tried with Cline** yet; it is the intended workflow, not a verified one. Review what comes back: the
gate stops a skill from writing unapproved, but it cannot tell you whether the skill is a good idea.
