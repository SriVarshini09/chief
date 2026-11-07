"""Chief's expressive voice: OpenAI's gpt-4o-mini-tts, used only when a key is set.

Without CHIEF_TTS_API_KEY nothing leaves the machine and the pages speak with the browser's own voice.
With it, only the words Chief is about to say are sent to the speech host. The text is never logged and
the audio is never written to disk: it goes straight back to the page that asked.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

MAX_TTS_CHARS = 1000  # Chief's spoken lines are short; the API allows 4096, this keeps each call cheap
MOODS = ("calm", "happy", "worried")

BASE_STYLE = (
    "You are Chief, a kind personal assistant speaking to the person you help. "
    "Speak like a warm, caring human friend: natural and relaxed, never robotic or announcer-like. "
    "Use gentle, natural pauses between sentences and a soft smile in the voice. "
    "When there is thanks to give, sound sincerely grateful, lightly, without gushing. "
    "Keep a calm, conversational pace and read times, names and numbers clearly."
)
MOOD_STYLE = {
    "calm": "Tone: calm, reassuring and gentle.",
    "happy": "Tone: bright and pleased, a little smile and lift in the voice, like sharing good news with a friend.",
    "worried": "Tone: softer and a little slower, caring and apologetic, steady so the listener feels looked after.",
}


def style(mood: str) -> str:
    """The `instructions` sent with each request: one warm voice whose feeling follows Chief's mood."""
    return f"{BASE_STYLE} {MOOD_STYLE.get(mood, MOOD_STYLE['calm'])}"


def mood_of(name: object) -> str:
    """The dashboard's moods (idle, waiting, thinking, happy, worried) folded into the three voices."""
    if name in ("happy", "worried"):
        return str(name)
    return "calm"


class Speaker(Protocol):
    enabled: bool

    def speak(self, text: str, mood: str) -> bytes: ...


class OpenAISpeaker:
    """Any OpenAI-compatible /audio/speech endpoint (OpenAI by default)."""

    media_type = "audio/mpeg"

    def __init__(self, base_url: str, api_key: str | None, model: str, voice: str, timeout: float = 20.0):
        self.base_url, self.api_key, self.model, self.voice, self.timeout = base_url.rstrip("/"), api_key, model, voice, timeout

    @classmethod
    def from_env(cls) -> "OpenAISpeaker":
        return cls(
            os.getenv("CHIEF_TTS_BASE_URL") or "https://api.openai.com/v1",
            os.getenv("CHIEF_TTS_API_KEY") or None,
            os.getenv("CHIEF_TTS_MODEL") or "gpt-4o-mini-tts",
            os.getenv("CHIEF_TTS_VOICE") or "marin",
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def request(self, text: str, mood: str) -> dict:
        body = {"model": self.model, "input": text, "voice": self.voice, "response_format": "mp3"}
        if not self.model.startswith("tts-1"):  # the older tts-1 models do not take instructions
            body["instructions"] = style(mood)
        return body

    def speak(self, text: str, mood: str) -> bytes:
        resp = httpx.post(
            f"{self.base_url}/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=self.request(text, mood),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.content
