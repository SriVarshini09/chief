"""Thin wrapper over any OpenAI-compatible host (open models via credits, Together, Groq, Ollama...).

Skills compute their findings deterministically and only ask the model to phrase them.
If no model is configured (or the call fails) the fallback text is used, so mock mode and
demo mode never depend on a network call.
"""

from __future__ import annotations

import logging

from chief.config import Config

log = logging.getLogger("chief.llm")

REQUEST_TIMEOUT = 20  # seconds: a slow model must not stall the stage; the fallback text answers instead


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.mode != "mock" and self.cfg.llm_api_key and self.cfg.llm_model)

    def complete(self, system: str, user: str, fallback: str) -> str:
        if not self.enabled:
            return fallback
        try:
            if self._client is None:
                from openai import OpenAI  # imported lazily: not needed in mock mode

                self._client = OpenAI(base_url=self.cfg.llm_base_url, api_key=self.cfg.llm_api_key)
            r = self._client.chat.completions.create(
                model=self.cfg.llm_model,
                temperature=0.3,
                stream=False,  # Cline's API streams by default; we parse one JSON reply
                timeout=REQUEST_TIMEOUT,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            return (r.choices[0].message.content or "").strip() or fallback
        except Exception as exc:  # network, quota, model errors: never break the loop
            log.warning("LLM call failed, using fallback: %s", exc)
            return fallback

    def chat_with_tools(self, messages: list[dict], tool_schemas: list[dict]) -> dict | None:
        """One OpenAI-style tool-calling turn. Returns the assistant message as a plain dict:
        {"role": "assistant", "content": str | None, "tool_calls": [{"id", "type": "function",
        "function": {"name", "arguments": <JSON string>}}]}  (no "tool_calls" key when there are none).
        Returns None when no model is configured or the call fails, so callers can fall back."""
        if not self.enabled:
            return None
        try:
            if self._client is None:
                from openai import OpenAI  # imported lazily: not needed in mock mode

                self._client = OpenAI(base_url=self.cfg.llm_base_url, api_key=self.cfg.llm_api_key)
            kwargs = {"tools": tool_schemas} if tool_schemas else {}
            r = self._client.chat.completions.create(
                model=self.cfg.llm_model, temperature=0.2, stream=False, timeout=REQUEST_TIMEOUT, messages=messages, **kwargs
            )
            msg = r.choices[0].message
            out: dict = {"role": "assistant", "content": msg.content}
            calls = [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in (getattr(msg, "tool_calls", None) or [])
            ]
            if calls:
                out["tool_calls"] = calls
            return out
        except Exception as exc:  # network, quota, model errors: never break the loop
            log.warning("LLM tool-calling call failed: %s", exc)
            return None
