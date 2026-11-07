# Launch checklist

What has to be true before Chief goes to customers. Checked on 2026-11-07 by reading the code, running every
question against broken services, a security review, a fresh install from GitHub as a new user, and research on the
outside services Chief depends on.

Legend: [x] done, [ ] to do. **Blocker** = don't launch without it.

## Done

- [x] One slow service crashed every answer ("Sorry, something went wrong"). Now: "I couldn't reach your calendar just
      now, so I didn't finish that. Nothing was changed." Tested for calendar, email and maps.
- [x] Places answers ("Sakura, 120 m away") were saved to disk. Now kept in memory only.
- [x] Links on the pages: only `https:`, `http:` and `tel:` can be opened.
- [x] Chief asks "Can I use your live location?" (Allow / Not now) before the browser asks; live location follows you
      until you switch it off; Telegram live location works.
- [x] **Slack:** only `SLACK_USER_ID` can approve or skip. Others' clicks are ignored.
- [x] **Web token:** short or guessable tokens are refused at start (`demo123` included); 20 wrong tokens from one
      address in 5 minutes are refused for a while; a warning when listening beyond this computer.
- [x] **Approval cards** name the event they change ("Standup", Wed Oct 21, 10:00 AM), show new notes in full, and say
      guests aren't emailed. Same words on the web page and Telegram.
- [x] **"yes" / "sure"** only approve the card you were just shown, and only for 30 minutes. Older cards need their button.
- [x] **"undo"** right after "say undo" reverses the change, even with another card waiting (it used to skip that card).
- [x] **Token out of the address bar:** pages keep it for the tab and remove it from the URL; a reload uses an
      HttpOnly, SameSite=Strict cookie that can only read, never approve or change anything.
- [x] **Safety headers:** no framing (no click tricks on Approve), no referrer.
- [x] **Diary on disk** (`.chief/activity.jsonl`) no longer keeps inbox briefs or AI answers, keeps only a short first
      line of cards, and rotates at 2 MB.
- [x] **Versions:** `requirements-lock.txt` holds the exact tested versions; unsafe `python-multipart` releases excluded.
- [x] **Calendar changes never email guests** (`send_updates: none`, checked against Composio's real schema).

## Blockers

1. [ ] **Decide how customers get Chief.** Today it is a one-person app: one web token, one Telegram owner, one
       Composio account (`CHIEF_USER_ID` defaults to `chief-demo`). Either each customer runs their own copy
       (needs an easy installer), or we host it and add real accounts: sign-in, one Chief per person, data kept apart.
2. [ ] **Google sign-off for Gmail.** Reading Gmail for other people needs our own verified Google OAuth app and a
       yearly CASA security assessment (weeks; reported cost from about $540 to $15k+ a year). Until then: stay a
       "testing" app with a short list of named test users.
3. [ ] **Map services.** The free OpenStreetMap servers forbid apps with many users (about 1 request a second for the
       whole app). Switch to a paid provider (LocationIQ, Geoapify, or Google Places) or host our own.
4. [ ] **Privacy policy and terms.** None exist. Telegram requires a privacy policy for bots; India's DPDP rules
       (notice, consent, deletion) apply from May 2027. Add "delete my data".
5. [ ] **Hosting.** Chief runs on a laptop. Customers need an always-on server with HTTPS (phones only share location on
       https), automatic restart, and backups of the state folder.

## Should fix before launch

6.  [ ] Waiting approvals are lost when Chief restarts.
7.  [ ] No error reporting: if Chief breaks for a customer, we won't know. Add monitoring.
8.  [ ] No AI model is connected, so questions outside the built-in ones get "needs an AI model". Decide.
9.  [ ] A "yes" can still approve a card shown on another screen (web vs Telegram share one conversation).
10. [ ] Telegram first-run setup trusts whoever messages the bot first; ask the operator to confirm the name.
11. [ ] Skills are trusted code: a skill could reach past its read-only tools. Document it before inviting outside skills.

## Fresh install (found by installing from GitHub as a new user)

12. [x] `run --once`: Enter now means "decide later" (nothing remembered); only "n" skips for good, and says how to undo it.
13. [x] The sample calendar and mail follow today's date, so the demo phrases work any day; meeting prep says why when
        there's nothing to prep.
14. [x] README: one install path; a small `web` extra (22 s instead of 97 s); fresh test count; Composio line corrected;
        the files example uses the demo folder.
15. [x] `doctor` in mock mode reports the web page's packages and port as warnings; the demo launcher still requires them.
16. [x] `{"text": null}` is refused; emoji get a kind answer; the brief and the greeting skip meetings that already ended;
        the sample invoice is due 9 days from today.
17. [x] Tests write only to temporary folders; the stray tracked log file is removed.

Re-checked from a clean copy following the README: install 22 s, 947 tests pass, `run --once` / `--approve`, `doctor`,
the weak-token refusal and `chief web` all behave as described.

## Test with real accounts and people

18. [ ] Not yet tried on a real account: moving and cancelling a meeting, drafts, replying in a thread.
19. [ ] Not yet tried on a real phone: Telegram buttons, Share my location / live location, the voice, the phone page.
20. [ ] A small beta: 3–5 busy people for a week; fix what confuses them.

## Booking: what's actually possible

- **Hotels: possible now.** LiteAPI is self-serve with a free sandbox and makes real bookings; the guest pays on
  LiteAPI's own payment page. (Amadeus closed its self-service APIs in July 2026.)
- **Restaurants in India:** Swiggy Dineout has an official booking connection for AI agents, invite-only:
  builders@swiggy.in.
- **Restaurants in the US:** OpenTable, Resy and Tock only work with approved partners. Until approved, Chief gets
  everything ready and opens the booking page.
