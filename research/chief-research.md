# Chief: research brief

Built from public docs, announcements and open-source repos only. No closed product was decompiled or reverse-engineered.
Several details come from third-party write-ups; those are marked "unverified".

## 1. What the existing products do

### Meta Muse (closed; public announcement only)
Source: https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/
- Keeps working after the app is closed, returns when something changes or it needs approval.
- Asks before sensitive actions (sending email, purchases). A separate "Sentinel" agent on the same VM must approve anything sent to the internet.
- Per-app access levels (e.g. email read-only vs read and send), revocable at any time.
- Memory: remembers details mentioned once, makes unprompted suggestions, supports "forget this".
- Full audit trail of actions and permissions.
- Not disclosed: how tasks are scheduled or triggered.

Copy for Chief: approval cards, an activity log, per-source read vs write permissions, a "forget" command.

### Grok Automations / Tasks (closed; third-party write-ups, unverified)
Sources: https://knightli.com/en/2026/07/18/grok-automations-vs-chatgpt-tasks-vs-claude-scheduled-tasks/ , https://mindstudio.ai/blog/grok-automations-scheduled-tasks-email-triggers
- Plain-language job description, plus a schedule trigger (one-time, daily, weekday, weekly...) or a mail trigger (sender/subject filters).
- Each run is a fresh conversation with the same instructions and current data; run history is kept.
- Tasks that delete, send or modify external data should keep manual approval.

Copy for Chief: a job = {instruction, trigger, notify rule, run history}. Fresh run each time, state kept in a file.

### Dot by New Computer (closed; secondary sources, unverified)
Sources: https://www.langchain.com/blog/customers-new-computer , https://notes.andymatuschak.org/zBmgU9c2rjvApZvTa68YfAr
- Memory creates structured records (with status and date fields) before retrieval, so questions about unfinished tasks or upcoming plans are answerable.
- Retrieval combines semantic search, keyword search and metadata filters.
- A competitor's blog claims Dot is winding down. Not independently confirmed, so verify before citing.

Copy for Chief: store memories as small structured records ({what, status, date, source}), not raw chat text.

## 2. Open-source projects to study (and reuse ideas from)

### OpenClaw (MIT; MIT claim from third-party guide, verify in repo LICENSE)
Sources: https://clawdocs.org/architecture/heartbeat , https://vallettasoftware.com/blog/post/openclaw-github
- Heartbeat: a periodic agent turn with no user prompt. Each tick sees a scratch note as context.
- The agent decides whether to notify: replies `NO_REPLY` (silent) or sends alert text. This is the key proactive pattern.
- Guards skip a tick when the queue is busy or outside `activeHours`.
- Heartbeat is for periodic check-ins; anything needing its own cadence (e.g. "every morning") is a separate cron job.
- Skills are `SKILL.md` files with YAML frontmatter. Recipes exist for Gmail VIP checks and GitHub PR monitoring.
- Security note: treat third-party skills as untrusted; one report says many instances were exposed to the internet.

Copy for Chief: the `NO_REPLY` rule, `activeHours`, and the skill file with frontmatter.

### GAIA (theexperiencecompany/gaia)
Source: https://github.com/theexperiencecompany/gaia
- License: PolyForm Noncommercial 1.0.0. **This is not open source in the usual sense. Do not copy code.** Reading the architecture for ideas is fine.
- Structure: FastAPI + LangGraph backend, bots for Slack/WhatsApp/Telegram/Discord/iMessage, voice worker (LiveKit), local tunnel, Docker self-hosting.
- Agent tiers: a comms agent that only delegates, an executor agent, and one subagent per integration with scoped tools.
- Workflows: plain-English, scheduled or event-triggered (new email, calendar change, GitHub commit...).
- Memory: user-viewable, editable, exportable; email and document bodies fetched on demand, not stored.

Copy for Chief (ideas only): scoped tools per skill, fetch-on-demand for sensitive data, user-editable memory.

### Kai (simonschubert/kai)
Source: https://docsearch.algolia.com/mcp/docs/repo/simonschubert/kai
- Persistent memory and autonomous heartbeats, MCP support. Not yet checked in detail.

## 3. The sponsor tools: what the docs say

### Composio
Sources: https://docs.composio.dev/cookbooks/background-agent.md , https://docs.composio.dev/examples/standup-slackbot , https://docs.composio.dev/examples/email-triggered-agent
- Python client:
  ```python
  from composio import Composio
  composio = Composio()
  session = composio.create(user_id="user_123")
  tools = session.tools()
  session.execute(tool_slug, arguments=...)
  ```
- Managed auth by default; the agent connects accounts at runtime. Connect links are generated per user.
- Standup bot pattern (closest to Chief): a scheduled job posts a Slack message with buttons; the button handler is deterministic code; the agent runs only for the drafting step; the user confirms or edits before it posts. Per-member sessions use `manageConnections: false`.
- Email-triggered agent (Python): `GMAIL_NEW_GMAIL_MESSAGE` trigger, signed webhook verified with `composio.triggers.parse(...)`, dedupe on the raw payload id in SQLite, agent restricted to one tool (`GMAIL_FETCH_EMAILS`), instructions say to treat email as untrusted.
- The docs only show TypeScript for the Slack bot; port to Python.

Copy for Chief: deterministic button flow plus agent-only-for-reasoning, tool allow-listing per skill, webhook verify and dedupe.

### Parallel
Source: https://docs.parallel.ai/search/search-quickstart
- `pip install parallel-web`, env var `PARALLEL_API_KEY`.
  ```python
  from parallel import Parallel
  client = Parallel(api_key=os.environ["PARALLEL_API_KEY"])
  search = client.search(objective="...", search_queries=["...", "..."])
  for r in search.results:
      print(r.title, r.url, r.excerpts)
  ```
- Modes: default `advanced` (about 3s), `fast` (about 700ms, recommended for most agents), `turbo` (about 200ms). Shown in the REST example, so check the Python parameter name.
- Results contain `url`, `title`, `publish_date`, `excerpts`.
- Monitor API (https://docs.parallel.ai/monitor-api): hourly/daily/weekly monitors with webhooks. Docs are inconsistent on parameter names ("frequency" vs "cadence"), so check the V1 API reference first.
- A third-party source says the Task API runs 10-15 minutes. That is too slow for a live demo. Use Search for the demo, Monitor for the watch feature.

## 4. Patterns, ranked by value for the demo

1. **Heartbeat with a "say nothing" default** (OpenClaw). Chief only messages when it finds something worth the user's attention.
2. **Approval cards for every write** (Muse, Grok, Composio standup bot). Deterministic buttons, agent only for reasoning.
3. **Skill files** (OpenClaw). One file per skill with frontmatter plus a `run()` function; this is the "add a skill in about 20 lines" claim.
4. **Structured memory** (Dot). Small records with status and date, viewable and deletable.
5. **Activity log** (Muse). Every action Chief took, in plain text.
6. **Scoped tools per skill** (GAIA, Composio). Each skill gets only the tools it needs.
7. **Voice front end** (Composio voice-agent blog: https://composio.dev/blog/building-a-voice-agent-to-command-all-your-apps).

## 5. What to avoid
- Do not copy GAIA code (noncommercial license).
- Do not run community skills without reading them.
- Do not give the agent write access to email without an approval step.
- Do not rely on the Task API in a live demo.

## 6. Open questions to check before building
- Exact Python parameters for Parallel `mode` and for Monitor V1.
- Whether Composio has a Samsung Health or Google Fit toolkit (likely not; use a CSV).
- Which open-source model and host the organizers' credits cover, and how well it handles tool calls.
- OpenClaw repo license and current structure (read the repo itself).
