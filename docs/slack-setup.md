# Slack setup (approval cards in a channel)

Chief posts each proposal to a Slack channel with **Approve** and **Skip** buttons. It uses Socket
Mode, so there is no public URL, tunnel or webhook to host.

## 1. Create the app

1. Go to <https://api.slack.com/apps> and choose **Create New App**, then **From scratch**.
2. Name it `Chief` and pick your workspace.

## 2. Settings to turn on

| Where (left sidebar) | Setting |
| --- | --- |
| **Socket Mode** | **Enable Socket Mode** on. |
| **Basic Information**, then **App-Level Tokens** | **Generate Token and Scopes**. Name it `chief-socket` and add the scope `connections:write`. Copy the token (`xapp-...`). This is `SLACK_APP_TOKEN`. |
| **Interactivity & Shortcuts** | **Interactivity** on. With Socket Mode enabled, no Request URL is needed. |
| **OAuth & Permissions**, then **Bot Token Scopes** | Add `chat:write`. (Optional: `chat:write.public`, so the bot can post to a public channel without being invited.) |
| **OAuth & Permissions** | **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-...`). This is `SLACK_BOT_TOKEN`. |

Not needed: Event Subscriptions, slash commands, any other scope. Button clicks arrive through
Interactivity over the socket.

## 3. Pick the channel

Create or choose a channel, then in Slack run `/invite @Chief` there (unless you added
`chat:write.public`). Use the channel ID for `SLACK_CHANNEL`: open the channel, click its name,
and copy the ID at the bottom of **About** (looks like `C0123456789`). A name like `#chief` also
works for public channels.

## 4. Fill in `.env`

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL=C0123456789
```

`.env` is gitignored; never commit it.

## 5. Run

```
pip install -e .[live]
python -m chief slack
```

This connects Socket Mode, starts the heartbeat, and posts a card whenever a skill has something
worth saying. Tapping **Approve** runs the proposed actions through the approval gate; **Skip**
runs nothing and Chief won't ask about that situation again. The card is rewritten to show the
outcome, including any action error. A click on a card Chief no longer knows about (already
decided, or Chief restarted since it was posted) just marks the card as handled.

Anyone who can see the channel can click the buttons, so use a private channel.
