# Live tools (Composio + Parallel)

`CHIEF_MODE=live` uses `chief/tools/live.py` (`LiveTools`). Every method returns the same types as `MockTools`.
Written without a Composio or Parallel key, so the parts marked **unverified** need one run with real keys.

## Setup

```bash
pip install -e ".[live]"          # composio>=0.28 (not composio-core), parallel-web, slack-bolt, ...
```
`.env`: `COMPOSIO_API_KEY`, `CHIEF_USER_ID`, `PARALLEL_API_KEY`; optional `CHIEF_TIMEZONE`, `CHIEF_CALENDAR_ID`, `CHIEF_SEARCH_DOMAINS`, `CHIEF_HEALTH_CSV`.

Connect the Google test account once (prints a link, open it in a browser signed in to that account):
```bash
python -m chief.tools.composio_client gmail
python -m chief.tools.composio_client calendar
python -m chief.tools.composio_client --status
```
`python -m chief connect gmail` needs this hook in `chief/__main__.py` (not edited by this task):
```python
# before ap.parse_args(...):  if argv and argv[0] == "connect": ...
if (argv if argv is not None else sys.argv[1:])[:1] == ["connect"]:
    from chief.tools.composio_client import connect_main
    return connect_main((argv if argv is not None else sys.argv[1:])[1:])
```

## What was verified, and how

| Item | Status |
|---|---|
| `composio.sessions.create(user_id=, toolkits=[...], tools={toolkit: {"enable": [...]}})`, `session.execute(slug, arguments=)`, `session.authorize(toolkit).redirect_url`, `session.toolkits(toolkits=[..])` | verified in `composio` 0.28.0 source; `execute` returns `.data/.error/.result_type` and never retries |
| `Parallel(api_key).search(objective=, search_queries=, mode="fast", advanced_settings={"max_results", "source_policy": {"include_domains"}})`, result `.results[].title/.url/.excerpts` | verified in `parallel-web` 1.3.5 source and docs.parallel.ai |
| Slugs and top-level argument names below | from the Composio toolkit pages (docs.composio.dev/toolkits/googlecalendar, /gmail) |
| Inner shape of each tool's `data` | **unverified**, see below |

## Slugs and arguments used

Read session (cannot write or send):

| Chief method | Slug | Arguments sent |
|---|---|---|
| `list_events` | `GOOGLECALENDAR_EVENTS_LIST` | `calendarId` (`primary`), `timeMin`, `timeMax` (offset-aware ISO), `singleEvents=true`, `orderBy=startTime`, `maxResults=250`, `pageToken` (up to 4 pages) |
| `find_free_slots` | none: computed from `list_events` | see note |
| `fetch_emails` | `GMAIL_FETCH_EMAILS` | `query` (Gmail syntax), `max_results`, `verbose=true`, `user_id="me"` |
| `fetch_thread` | `GMAIL_FETCH_MESSAGE_BY_THREAD_ID` | `thread_id`, `user_id="me"` |
| `create_draft` | `GMAIL_CREATE_EMAIL_DRAFT` | `recipient_email`, `subject`, `body`, `thread_id` (if given), `user_id="me"` |

Write session (created only when an approved write runs):

| Chief method | Slug | Arguments sent |
|---|---|---|
| `patch_event` | `GOOGLECALENDAR_PATCH_EVENT` | `event_id`, `calendar_id` (required by the tool), plus `summary`, `description`, `location`, `start_time`, `end_time`, `timezone`. Other `fields` keys raise `ValueError` |
| `create_event` | `GOOGLECALENDAR_CREATE_EVENT` | `calendar_id`, `summary`, `start_datetime`, `end_datetime`, `timezone`*, `description`, `send_updates="none"` |
| `delete_event` | `GOOGLECALENDAR_DELETE_EVENT` | `event_id`, `calendar_id` |
| `send_email` | `GMAIL_SEND_EMAIL` | `recipient_email`, `subject`, `body`, `user_id="me"` |
| `send_email` with `thread_id` | `GMAIL_REPLY_TO_THREAD` | `thread_id`, `recipient_email`, `message_body`, `user_id="me"` |

\*only when `CHIEF_TIMEZONE` is set. `GMAIL_REPLY_TO_THREAD` is not in the original brief; it is used because `GMAIL_SEND_EMAIL` documents no `thread_id`.

`search_web` -> Parallel Search, `mode="fast"`, `advanced_settings.max_results`, and `advanced_settings.source_policy.include_domains` from the `include_domains` argument (LiveTools only; MockTools ignores it) or `CHIEF_SEARCH_DOMAINS`.
`read_health` reads a CSV (`CHIEF_HEALTH_CSV` or `data/sleep.csv`; format of `chief/health.py`) and **raises if it is missing**, so the audit skill fails loudly in live mode until the file exists. `post_slack` uses `slack_sdk` with `SLACK_BOT_TOKEN` + `SLACK_CHANNEL`.

Decisions worth knowing:
- `find_free_slots` is computed from `list_events` (not `FIND_FREE_SLOTS` / `FREE_BUSY_QUERY`) so the `[Async]` rule matches the mock and no extra response shape is needed.
- `list_events` skips all-day and cancelled events (they are not meetings and would inflate the heavy-day count).
- Times: Google datetimes become naive local; naive input is treated as local (system zone, or `CHIEF_TIMEZONE`).
- Safety: reads/drafts and writes use two separate Composio sessions with separate `tools` enable lists. Whether a write may run is still decided by `ApprovalGate`; `ScopedTools` hides write methods from skills.

## Check these with real keys (first run, in this order)

1. `python -m chief.tools.composio_client --status`, then connect both toolkits.
2. `LiveTools(cfg).list_events(...)` returns your events. If it returns `[]` for a busy calendar, the `data` nesting differs: print `session.execute("GOOGLECALENDAR_EVENTS_LIST", arguments={...}).data` and adjust `_find_list` keys in `live.py`.
3. `fetch_emails()` returns mail with sender, subject, snippet and time. The field names (`messageId`, `threadId`, `sender`, `subject`, `messageText`, `messageTimestamp`, `labelIds`) come from memory of the Composio output, not the docs.
4. `create_draft(...)` returns a non-null `draft_id`; check the draft in Gmail.
5. Approve one `create_event` and one `patch_event` in the test account. Check the event lands at the right local time: if Composio rejects the offset in `start_datetime`/`start_time` (e.g. `-07:00`), set `CHIEF_TIMEZONE` so a naive string plus `timezone` is the safer path (change `_when` in `live.py`).
6. `patch_event` accepts `start_time`/`end_time` (argument names from the docs, behaviour untested).
7. `search_web` with `include_domains=["caltrain.com"]`: confirm results are restricted and have excerpts.
8. On Windows, `CHIEF_TIMEZONE` needs the `tzdata` package (included in the `live` extra).
