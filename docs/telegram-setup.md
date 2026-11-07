# Telegram setup

Run Chief as a Telegram bot to get notifications, Approve and Skip buttons, and a morning brief on your phone. It needs no tunnel and
no public URL, but Chief has to keep running on a computer that stays on.

**Status: tested against a fake Telegram API only. It has never connected to real Telegram.** Update this line after the first real run.

## Set it up

You need to do steps 1 and 3 yourself in Telegram. Keep the token out of chat, screenshots and commits.

1. In Telegram, open **@BotFather**, send `/newbot` and follow the prompts. It gives you a bot token.
2. Put the token in `.env`:

   ```
   TELEGRAM_BOT_TOKEN=your-token-here
   ```

3. Find your numeric ID. Run this, then send your bot any message:

   ```powershell
   .venv\Scripts\python.exe -m chief telegram
   ```

   It replies with your ID and stops. Check that it is yours: the first person to message the bot gets the reply.
4. Add it to `.env`:

   ```
   TELEGRAM_USER_ID=your-numeric-id
   ```

5. Start Chief. It stays on Telegram until you press Ctrl+C:

   ```powershell
   .venv\Scripts\python.exe -m chief telegram
   ```

Without `CHIEF_MODE=live`, Chief uses the fake mock data, so you can try this before connecting a real account.

## What you get

- **Approvals:** each proposal arrives as a message in plain words, with Approve and Skip buttons. Typing "do it" or "skip" also works.
- **Questions:** send anything you would say on the phone page, such as "What's in my inbox?".
- **Check-ins:** Chief reviews your week every few hours and stays silent unless something is worth saying.
- **Morning brief:** sent once a day, at 8 AM by default. Set `CHIEF_BRIEF_HOUR` to another hour from 0 to 23, or to `off`. If Chief starts more
  than three hours after that hour, it skips that day's brief. You can ask for one at any time with "morning brief".

## Safety

- The bot acts only on your `TELEGRAM_USER_ID`, in your private chat with it. Anyone else who finds the bot gets no reply.
- Telegram stores what it delivers. Calendar and email text in your cards passes through Telegram's servers, so use the demo account until you are
  comfortable with that.
- Messages are sent as plain text with link previews off, so Telegram does not fetch links found in your email.
- A long email is split across several messages, and the buttons come after the last one, so you can read all of it before approving.
- Messages sent while Chief was off are dropped when it starts. An old "do it" cannot approve something new.
- A proposal does not survive a restart. Tapping a button from before one says it has expired, and nothing runs.

## Use it with the phone page

With both settings in `.env`, `python -m chief web` runs Telegram too, and a card shows in both places. Run `python -m chief telegram` only
when you want the bot without the web page. Do not run both commands at once: Telegram allows one reader per bot.

## Limits

- The bot reads text only. Voice messages are not supported yet.
