# Sign in and connect your accounts

Chief has a front door: a public landing page, a password sign-in, and a setup page where you connect
everything. You don't need to edit files or copy token links to get going.

| Page | Who can open it | What it does |
| --- | --- | --- |
| `/welcome` | anyone | Says what Chief does. Has **Get started** and **Sign in**. Shows no data. |
| `/signin` | anyone | **Create your account** the first time, then **Sign in** with email and password. |
| `/setup` | you, signed in | Shows Google, Telegram, location, the morning brief and which keys are set. |
| `/dashboard`, `/`, `/stage` | you, signed in or with the token link | Same as before. |

`chief web` prints the welcome link when it starts:

```
Welcome, sign in and connect your accounts: http://localhost:8765/welcome
```

## First run

1. Start Chief (`python -m chief web`) and open the `/welcome` link **on the same computer**.
2. Choose **Get started**, then type your name, email and a password (10 characters or more).
3. You land on `/setup`. Connect Google, and optionally Telegram and your location.

Chief works for one person. There is exactly one account. Today it is the same single owner as the
web token, the Telegram owner and the Composio user (`CHIEF_USER_ID`).

### Who may create the first account

Whoever creates the account owns this Chief, so the first account can be made only:

- **from a browser on the computer running Chief**, meaning the connection comes from a loopback
  address, the `Host` header is `localhost`, `127.0.0.1` or `[::1]`, and there are no proxy headers; or
- **with the web token**, meaning you opened the private `?token=` link Chief printed and went from
  there to `/signin`.

The address alone is not trusted, because a Cloudflare tunnel also connects from `127.0.0.1`.
Tunnelled requests carry the public host name and `Cf-Connecting-Ip` / `X-Forwarded-For`, so a
stranger reaching your tunnel URL sees "Create your account on your computer" and cannot create one.
A DNS-rebinding page fails the `Host` check, and other sites are stopped by the cross-site check below.

## How signing in works

- The owner is stored in `<state dir>/owner.json` (by default `.chief/owner.json`, which is git-ignored).
  It holds your name, email and a **`hashlib.scrypt` hash** of the password with a random 16-byte salt
  (n=2^14, r=8, p=1). The password itself is never written anywhere.
- A correct email and password set the existing `chief_token` cookie: HttpOnly, SameSite=Strict,
  and Secure on https (also behind a tunnel that sends `X-Forwarded-Proto: https`). It is a browser-
  session cookie, so it goes when the browser closes.
- A wrong email and a wrong password get the same answer: "That email and password don't match."
  Each wrong try waits longer (0.5 s, 1 s, 2 s, then 4 s). After 8 wrong tries from one address in
  15 minutes, sign-in answers 429 until the window passes, even for the right password.
- **Sign out** (dashboard top bar, setup page, sign-in page) clears the cookie and this tab's token.

### Cross-site requests are refused

Sign-in, account creation and sign-out POSTs are accepted only when:

- `Origin` (or, if missing, `Referer`) names this host. A request with neither is refused.
- `Sec-Fetch-Site`, when the browser sends it, is `same-origin`.
- The body is JSON. An HTML form on another site cannot send JSON without a preflight.

### How the pages get the token after a password sign-in

Chief's pages send the token in the `X-Chief-Token` header for every API call, and the cookie on
its own only allows reading (GET). After a password sign-in, the pages ask `GET /session` once and keep
the answer in `sessionStorage`, the same place a token link puts it.

`/session` is safe to expose this way because:

- it needs the cookie (or the token), and the cookie is SameSite=Strict, so other sites' requests
  arrive without it;
- it needs the custom header `X-Chief-Session: 1`, which another site cannot add without a CORS
  preflight, and Chief never approves one;
- it refuses when `Sec-Fetch-Site` is not `same-origin` or `Origin` names another host;
- it answers with `Cache-Control: no-store`.

A script already running inside a Chief page could read the token from `sessionStorage` anyway, so
`/session` gives it nothing new.

### Token links still work

`/?token=...`, `/dashboard?token=...`, the installed phone app and the `X-Chief-Token` header work
exactly as before. Opening `/dashboard` or `/setup` in a browser while signed out now goes to
`/signin` instead of showing a JSON error. Scripts and `/` itself still get the plain 401.

## The setup page

- **Google (Gmail and Calendar).** In mock mode it says **Sample data** and the connect buttons are
  hidden. In live or demo mode, **Connect Gmail** / **Connect Calendar** ask Chief to make a Composio
  connect link on the server (`ComposioGateway.connect_link`, the same one `python -m
  chief.tools.composio_client gmail` uses), and open it in a new tab. The card checks every few
  seconds and shows **Connected** when Composio says so. Without `COMPOSIO_API_KEY` it says so.
- **Telegram.** Whether `TELEGRAM_BOT_TOKEN` and a numeric `TELEGRAM_USER_ID` are set, and the steps in
  [telegram-setup.md](telegram-setup.md).
- **Location.** The same "Can I use your live location?" consent as the phone page. Chief keeps it
  in memory only. **Stop sharing** forgets it.
- **Morning brief and time zone.** Shows `CHIEF_BRIEF_HOUR` and `CHIEF_TIMEZONE` and suggests the
  browser's time zone. They are read only when Chief starts, so change them in `.env` and restart.
  The page does not save them.
- **Keys in .env.** Names and whether each one is set, never the values. The page never asks for a key.

## Forgot your password

On the computer running Chief, delete `owner.json` from the state dir (`.chief/owner.json` by
default), then open `http://localhost:<port>/welcome` and create the account again. Nothing else is lost.

## Not built yet

- Several accounts. Hosting Chief for more than one person is a separate decision. `owner.py` keeps one
  record, and the page flow would stay the same with one record per person.
- Revoking a signed-in browser on its own. Sign out clears that browser's cookie. To lock out every
  browser and installed phone, change `CHIEF_WEB_TOKEN` and restart.
