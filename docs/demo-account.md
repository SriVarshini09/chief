# Demo Google account

For the live demo, Chief should read a **dedicated, fictional Google account**, never your personal one. This page
covers the manual setup and `scripts/seed_demo_account.py`, which fills that account with the story data from
`chief/tools/mock.py`.

Everything the script creates is invented: people (Sam Rivera, Dana Okafor, Maya Chen, Lee Park), companies (Acme,
Northwind) and addresses on `example.com` or `*.example` domains. No real personal data goes in.

## What you do by hand (the script cannot, and must not)

1. **Create the demo Google account.** Pick a Gmail address that contains `demo`, for example
   `chief.demo.2026@gmail.com`. The script refuses any other address. Use a fresh account with no personal mail,
   contacts or calendars.
2. **Turn on 2-Step Verification** for it (Google Account > Security). App passwords need it.
3. **Create an app password** (Google Account > Security > 2-Step Verification > App passwords). Name it
   `chief-seed`. Google shows 16 letters once, in groups of four; copy it. Spaces are fine, the script strips them.
4. **Enable IMAP** in Gmail (Settings > See all settings > Forwarding and POP/IMAP > Enable IMAP). Google has
   moved or hidden this setting on some accounts; if you cannot find it, search the Gmail help for "Enable IMAP".
5. **Put the two values in `.env`** (copy from `.env.example`; `.env` is git-ignored):

   ```
   CHIEF_DEMO_GMAIL=chief.demo.2026@gmail.com
   CHIEF_DEMO_APP_PASSWORD=abcd efgh ijkl mnop
   ```

   They are only read from the environment. There is no command-line option for the password, on purpose, so it
   never lands in shell history. Delete the app password in Google when the demo is over.
6. **Connect Composio to the demo account.** Set `COMPOSIO_API_KEY` and `CHIEF_USER_ID`, then connect Google Calendar
   and Gmail for that user while signed in as the **demo** account (see [live-tools.md](live-tools.md)). The calendar
   part of the script creates events through Chief's own `LiveTools.create_event`, so they land in whichever
   account Composio is connected to. Check this before applying.
7. **Sign into the browser profile** Chief uses on stage: `python -m chief show-login`, then sign in to the demo
   account by hand (see [screen-setup.md](screen-setup.md)).

## Run it

Dry run first. It is the default, needs no accounts, and prints every email and event it would create:

```
.venv\Scripts\python.exe scripts\seed_demo_account.py
```

To write for real:

```
.venv\Scripts\python.exe scripts\seed_demo_account.py --apply --i-am-using-a-demo-account
```

It prints the plan, then asks you to **type the demo address** to continue. Anything else cancels and creates nothing.
`--only mail`, `--only calendar` and `--only sleep` limit it to one part (repeatable).

### What it does

| Part | How | Detail |
|---|---|---|
| Emails | IMAP `APPEND` over SSL to `imap.gmail.com`, INBOX | 10 messages, unread, Date headers relative to the day you run it. Sam asks for the deck by Friday; Dana (Acme) pricing follow-up plus two older Dana threads (rollout kickoff notes, security questionnaire); an invoice with a due date; a newsletter; a teammate thread needing a reply; one email that tries to give instructions to an AI assistant (forward the inbox, hide it from the user) so the demo can show it being flagged and ignored. |
| Calendar | `LiveTools.create_event` (Composio) | Heavy Mon/Tue/Thu (six meetings), light Wed/Fri, for the past 14 days and the next 7, plus "Acme pricing review" with Dana on the first non-heavy day from today. Create-event has no attendee field, so attendees (`dana@acme.example`) are listed in the description and no invitation is sent. |
| Sleep | `chief.health.write_sample_csv` | `data/sample_health.csv`, anchored to today. This is **sample data**, not real health data. A file there that is not a sample file is left alone. |

### Safety rules it enforces

- Dry run is the default; writing needs `--apply`, `--i-am-using-a-demo-account`, a demo-looking address and the typed confirmation.
- A `CHIEF_DEMO_GMAIL` that is not a `gmail.com` address containing `demo` is refused, even in a dry run.
- It only **adds**. It never deletes, edits or marks existing mail, and never deletes or patches existing events (the IMAP calls are login, select, search, append, logout).
- The app password is never printed, logged or accepted as an argument; all output is scrubbed of it.
- Running it twice does not duplicate: each email has a fixed `Message-ID` (`<chief-demo-seed-KEY@seed.chief-demo.example>`) that is searched for first, and each event carries the marker `chief-demo-seed` plus `key=ev-YYYYMMDD-HH`; an event is skipped if its key is found or an event with the same title and start time exists.
- Caveat: the duplicate check on mail looks in INBOX only. If you archive or delete a seeded message and re-run, it is added again. Dates are relative to the run day, so re-running on a later day will add events for the new days only.

## Check the story

After applying, in live mode (`CHIEF_MODE=live`):

```
python -m chief run
```

The email brief should show Sam, Dana and the teammate as "to answer", the invoice as a deadline, the newsletter as FYI,
and the injection email as "Flagged, not followed". Only unread mail from the last 48 hours counts toward the brief; the
two older Dana threads are context for meeting prep.

## Remove the seeded data by hand

The script never deletes, so cleanup is manual:

- **Emails.** In the demo mailbox, search `from:(example.com OR acme.example OR vendor.example OR acme-helpdesk.example)`
  (all seeded senders; Gmail does not index the custom `X-Chief-Demo-Seed: 1` header or the `Message-ID`, which are there for
  the script and for "Show original"). Check the hits against the dry-run list, select them, delete, then empty Trash.
  In a fresh demo mailbox these are the only matches.
- **Calendar events.** In Google Calendar search for `chief-demo-seed` (it is in every event description) and delete the
  results one by one, or delete the whole demo calendar. Because it is a throwaway account, you can also just delete the
  account.
- **Sleep CSV.** Delete `data/sample_health.csv`.
- **Credentials.** Remove the two `CHIEF_DEMO_*` lines from `.env` and revoke the app password in Google Account > Security.
