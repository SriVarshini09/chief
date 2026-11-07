# Chief's voice

Chief speaks on the Garden dashboard (`/dashboard`) and the phone page (`/`). It has two voices.

## The free voice (always on)

With no key, Chief uses your browser's own speech voices, and nothing leaves your device. The page picks
the most natural voice the browser has, such as Microsoft Aria, Jenny or Ava "Online (Natural)" in Edge,
"Google UK English Female" in Chrome, or Samantha on Apple. It speaks a little slower and softer than the
default and pauses briefly between sentences. It also changes with Chief's mood: brighter when something
went well, slower and lower when something went wrong.

To choose another voice, use the slider button next to the speaker button on the dashboard, or the
**Voice** list on the phone page. Press **Test voice** to hear it. Your choice is saved in this browser
(localStorage `chief-voice-name`).

Edge on Windows has the most natural free voices. On a phone, the voice list depends on the phone.

## The natural voice (optional)

For a warmer, more expressive voice, give Chief an OpenAI key. Chief then uses OpenAI's `gpt-4o-mini-tts`
model, which takes style `instructions`. Chief asks for a warm, friendly delivery with natural pauses and
light gratitude, with feeling that follows its mood (calm, happy or worried).

Add these to `.env`, then restart `python -m chief web`:

```
CHIEF_TTS_API_KEY=sk-...                    # required; without it the natural voice is off
# CHIEF_TTS_VOICE=marin                     # OpenAI recommends marin or cedar; also coral, shimmer, nova, sage, alloy, ash, ballad, echo, fable, onyx, verse
# CHIEF_TTS_MODEL=gpt-4o-mini-tts
# CHIEF_TTS_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible /audio/speech host
```

`/status` then reports `"tts": true`. The voice list shows **Chief's natural voice (recommended)**, which
is chosen unless you have picked a browser voice. The page sends each reply to the token-protected
`POST /tts` route and plays the audio it gets back. On the dashboard, the kitten's mouth follows how loud
the audio is.

If anything goes wrong (no key, a network error, a reply longer than 1,000 characters, or no answer within
8 seconds), the browser's voice says the words instead. Nothing breaks without a key.

Note: OpenAI's docs say `gpt-4o-mini-tts` shuts down on January 6, 2027. Point `CHIEF_TTS_MODEL` at its
replacement before then.

## Cost

OpenAI's pricing page lists `gpt-4o-mini-tts` at $0.60 per million text input tokens and $12.00 per million
audio output tokens. OpenAI estimates this comes to about **1.5 cents per minute of speech**. Chief's
replies are a few seconds each, so a day of normal use costs a few cents. A 1,000-character cap on each
request keeps a single call cheap. Check https://platform.openai.com/docs/pricing for current prices.

## Privacy

- **No key:** speech is made in your browser. No text goes to OpenAI.
- **With a key:** the words Chief is about to say are sent to OpenAI (or to your `CHIEF_TTS_BASE_URL`).
  Only those words are sent: not your mail, your calendar, or what you said. These replies can still
  mention names, meeting titles or places, so set the key only if you are comfortable with that.
- Chief never logs the text and never writes the audio to disk. The audio goes straight back to the page
  with `Cache-Control: no-store`.
- `/tts` needs the access token in the request (`X-Chief-Token`, `?token=` or a bearer header). The
  read-only cookie is not enough, so another site cannot spend your key.
