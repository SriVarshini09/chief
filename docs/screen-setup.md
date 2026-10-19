# Real windows on stage

For the live demo the audience should see the real apps move: ask for a morning brief and Gmail opens, ask about
your day and Google Calendar opens on that day, ask for research and the source pages open. `chief/screen.py` does
this. It listens to the same activity stream as the Stage page (`rt.bus`) and opens browser windows. Composio and
Parallel still do the actual work; this is only a presentation layer.

## Safety

- The layer **only opens URLs**, and (optional DevTools mode) **navigates or reloads a tab it already shows**. It never clicks,
  types, sends or changes anything in Gmail, Calendar or any page.
- Real changes to your calendar or mail happen **only through the approval gate**. The layer reacts to the event that
  follows an approved write by refreshing the view; it cannot cause the write.
- Web pages come from search results, which are untrusted. Only plain `http(s)` links are opened (no `javascript:`,
  `file:`, `data:`, `chrome:`, links with embedded passwords, or truncated URLs), at most three per search, and never
  an internal target: `localhost`, `*.localhost`, `*.local`, `*.internal`, one-word hostnames, `127.0.0.0/8`,
  `10/8`, `172.16/12`, `192.168/16`, link-local (`169.254/16`, `fe80::/10`), unique-local IPv6, `0.0.0.0`, carrier-grade
  NAT, and the odd spellings of those (`2130706433`, `0x7f.1`, `127.1`, `::ffff:127.0.0.1`). The check is on the
  name or number only (no DNS lookup), so a public name that secretly points at a private address would not be caught.
- It uses a **dedicated browser profile**, so only the test Google account is shown and your personal accounts and
  tabs never appear. Chief refuses to start if `CHIEF_SHOW_PROFILE_DIR` is, contains, or sits inside the default
  Chrome/Edge/Chromium/Brave profile folder. Use a throwaway test account, never a personal one.
- It is **off by default**: no browser is started unless `--show-windows` or `CHIEF_SHOW_WINDOWS=1` is given.
- Events marked `detail["cached"]` or `detail["simulated"]` (replayed or made-up data) never open anything: the data
  did not come from live pages, so the screen would be lying.

## Turn it on

```
python -m chief run --show-windows      # or: python -m chief web --show-windows
```

or set `CHIEF_SHOW_WINDOWS=1` in `.env`. All settings are in `.env.example` (`CHIEF_SHOW_*`).

## 1. Sign into the test account (once)

```
python -m chief show-login
```

A Chrome (or Edge) window opens on the dedicated profile (`.chief/browser-profile` unless you set
`CHIEF_SHOW_PROFILE_DIR`). Sign in to the **test Google account by hand**, check Gmail and Calendar load, then close
the window. The login stays in that profile. Chief never types credentials. This window is started without the
DevTools port, because Google's sign-in can be stricter with automation flags present.

Note: `python -m chief reset` deletes `.chief`, including the default profile, which signs the test account out.
Set `CHIEF_SHOW_PROFILE_DIR` to a folder outside `.chief` to keep the login across resets.

## 2. Check it before going on stage

```
python -m chief show-demo            # real windows; needs the test account signed in (step 1)
python -m chief show-demo --dry-run  # prints the URLs only, no browser
```

The demo replays a short made-up set of events: Gmail inbox, Calendar on today, two source pages, then a Calendar
refresh. It touches no account; the "calendar update" in it is a canned event, not a real change.

## What opens when

| Event (from the activity stream) | Window |
| --- | --- |
| `fetch_emails`, `fetch_thread` (read) | Gmail inbox |
| `list_events`, `find_free_slots` (read) | Calendar on the day asked about (else the day of the first result, else today) |
| `create_event`, `patch_event`, `delete_event` (write, after approval) | Calendar on that day, refreshed |
| `create_draft` / `send_email` (write) | Gmail Drafts / Sent |
| `search_web` (read) | the top three result pages, first in a new window, others as tabs of it |

The table is `ROUTES` in `chief/screen.py`; add a row to show something else.

Throttling: the same page is never opened twice within 30 s; at most 6 windows per request (a request starts when
you speak, or after 90 s of quiet); refreshes are at least 2 s apart.

## Window size and a second monitor (Stage on one, real apps on the other)

```
python -m chief show-layout
```

lists the monitors it detects (Windows only) and prints `CHIEF_SHOW_WINDOW_POS` / `CHIEF_SHOW_WINDOW_SIZE` values for
each secondary monitor (or, with one monitor, the right half of it). Put those in `.env`.

By hand: `CHIEF_SHOW_WINDOW_SIZE=1280x800` and `CHIEF_SHOW_WINDOW_POS=1920,0` (x,y of the top-left corner in desktop
pixels). Coordinates run across all monitors: a monitor to the right of a 1920-wide main display starts at x=1920,
one to the left starts at x=-1920 (use `-1920,0`).

Two things are placed separately. The **Stage page** is a page in your own browser: open it and drag that window to the
monitor you want (Chief does not move it). The **real windows** are Chief's own, placed by the two variables above:
every launch passes them, and in DevTools mode every window and tab Chief creates after the first gets the same size and
position. If a window lands on the wrong screen, adjust the position; Windows keeps the last position of a profile's
windows, so drag one once and close it to teach the profile. On displays with mixed scaling (125%/150%) the pixel
numbers can be off by the scale factor; nudge them and check with `show-demo`.

## Optional: reload Calendar in place (DevTools)

By default every launch opens a new window or tab, and a write does not open a duplicate (Google Calendar's page
normally updates itself within seconds). To reload the Calendar tab in place instead, set `CHIEF_SHOW_DEBUG_PORT=9222`.
Chief then starts the dedicated profile with `--remote-debugging-port=9222` and, once it is up (it waits a few seconds
after launching), keeps **one Gmail tab and one Calendar tab**: a new read navigates that tab (another day) or just brings
it forward (same page), and about 1.5 s after an approved write event the Calendar tab is reloaded in place so the
audience sees the change. Search result pages and the first Gmail/Calendar window are created through DevTools with
the configured size and position. This is plain standard library code (a tiny websocket client), no Playwright and
nothing to download. It only ever sends `Page.navigate`, `Page.reload`, `Target.createTarget` and a tab activation, so
it cannot click, type or change anything in a page.

If the port cannot be reached (for example the dedicated profile was already open without it), Chief prints one line
saying so and carries on opening plain windows as without the port.

Trade-offs: the port is bound to `127.0.0.1`, but any program on this computer can then drive that browser profile,
which is signed into the test account, so only use it on a machine you trust. The browser must be started by Chief
with the port: if the dedicated profile is already open without it, close it first. Chrome ignores the debugging
port for its default profile, which is another reason for the dedicated one.

## Known limits

- Verified: the event-to-URL mapping, de-duplication, caps, http(s) and internal-address filtering, the profile
  guard and browser arguments (unit tests, no browser), and the DevTools client against a real headless Chrome with a
  throwaway profile (new target with bounds, reload). **Not verified here:** a real login to Google, the actual
  Gmail/Calendar pages, `--window-position` on a real second monitor, whether Google's pages behave with the debugging
  port on, the Gmail/Calendar tab reuse against the real tabs (tested against a fake endpoint), and `show-layout` on a
  multi-monitor setup (it ran on a one-monitor machine). Rehearse with `show-demo` before stage.
- Without the DevTools port, a calendar refresh after a write relies on Calendar's own live updates. If the screen is
  stale, turn the port on.
- If the dedicated profile's browser is already running, a new launch hands its URL to that process and window
  size/position flags may not apply to existing windows.
- Google may show a "verify it's you" prompt on a new profile; resolve it by hand during `show-login`.
- Slack-only mode (`python -m chief slack`) builds its own runtime and does not have the flag yet.
- The Windows paths searched are Chrome then Edge under Program Files and `%LOCALAPPDATA%`; set
  `CHIEF_SHOW_BROWSER` for anything else.
