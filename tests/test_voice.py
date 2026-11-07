"""Chief's voice: the natural voice behind /tts, the browser-voice fallback, and the warmer words."""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from chief.dashboard import opening  # noqa: E402
from chief.gate import results_text  # noqa: E402
from chief.intent import handle_utterance  # noqa: E402
from chief.web import tts  # noqa: E402
from chief.web.app import DASHBOARD_PAGE, PAGE, PET_SCRIPT, VOICE_SCRIPT, create_app  # noqa: E402
from chief.web.tts import MAX_TTS_CHARS, OpenAISpeaker  # noqa: E402

TOKEN = "s3cret-test-token"
H = {"X-Chief-Token": TOKEN}
MP3 = b"ID3\x04\x00fake-mp3-audio" * 20


class FakeSpeaker:
    media_type = "audio/mpeg"

    def __init__(self, enabled=True, fail=False, audio=MP3):
        self.enabled, self.fail, self.audio, self.calls = enabled, fail, audio, []

    def speak(self, text, mood):
        self.calls.append((text, mood))
        if self.fail:
            raise RuntimeError("upstream down")
        return self.audio


def app_with(rt, speaker):
    return TestClient(create_app(rt, TOKEN, speaker=speaker))


# ---- the route ---------------------------------------------------------------------------------------


def test_without_a_key_it_says_503_and_status_says_no(rt, monkeypatch):
    monkeypatch.delenv("CHIEF_TTS_API_KEY", raising=False)
    client = TestClient(create_app(rt, TOKEN))  # the real speaker, built from an environment with no key
    r = client.post("/tts", json={"text": "Hello there."}, headers=H)
    assert r.status_code == 503 and "CHIEF_TTS_API_KEY" in r.json()["detail"]
    assert client.get("/status", headers=H).json()["tts"] is False


def test_a_fake_speaker_gives_audio_and_status_says_yes(rt):
    speaker = FakeSpeaker()
    client = app_with(rt, speaker)
    assert client.get("/status", headers=H).json()["tts"] is True
    r = client.post("/tts", json={"text": "  All done. Thank you!  ", "mood": "happy"}, headers=H)
    assert r.status_code == 200 and r.content == MP3
    assert r.headers["content-type"] == "audio/mpeg" and r.headers["cache-control"] == "no-store"
    assert speaker.calls == [("All done. Thank you!", "happy")]


@pytest.mark.parametrize("mood, heard", [("happy", "happy"), ("worried", "worried"), ("idle", "calm"), ("thinking", "calm"), (None, "calm"), (7, "calm")])
def test_moods_fold_into_three_voices(rt, mood, heard):
    speaker = FakeSpeaker()
    app_with(rt, speaker).post("/tts", json={"text": "Hi.", "mood": mood}, headers=H)
    assert speaker.calls[-1][1] == heard


def test_the_token_is_required_and_the_cookie_is_not_enough(rt):
    speaker = FakeSpeaker()
    client = app_with(rt, speaker)
    assert client.post("/tts", json={"text": "Hi."}).status_code == 401
    assert client.post("/tts", json={"text": "Hi."}, headers={"X-Chief-Token": "nope"}).status_code == 401
    client.cookies.set("chief_token", TOKEN)  # a cookie only reads; it cannot spend your key
    assert client.post("/tts", json={"text": "Hi."}).status_code == 401
    assert speaker.calls == []
    assert client.post("/tts", json={"text": "Hi."}, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": None}, {"text": 42}])
def test_nothing_to_say_is_refused(rt, body):
    speaker = FakeSpeaker()
    assert app_with(rt, speaker).post("/tts", json=body, headers=H).status_code == 400
    assert speaker.calls == []


def test_text_is_capped(rt):
    speaker = FakeSpeaker()
    client = app_with(rt, speaker)
    assert client.post("/tts", json={"text": "a" * MAX_TTS_CHARS}, headers=H).status_code == 200
    r = client.post("/tts", json={"text": "a" * (MAX_TTS_CHARS + 1)}, headers=H)
    assert r.status_code == 413 and len(speaker.calls) == 1
    assert MAX_TTS_CHARS <= 4096  # OpenAI's own limit for one request


def test_a_failing_speaker_is_a_502_that_never_repeats_the_text(rt, caplog):
    client = app_with(rt, FakeSpeaker(fail=True))
    with caplog.at_level(logging.DEBUG):
        r = client.post("/tts", json={"text": "my private words"}, headers=H)
    assert r.status_code == 502 and "RuntimeError" in r.json()["detail"]
    assert "my private words" not in r.text and "my private words" not in caplog.text
    assert app_with(rt, FakeSpeaker(audio=b"")).post("/tts", json={"text": "Hi."}, headers=H).status_code == 502


def test_no_audio_is_written_to_disk(rt, tmp_path, monkeypatch):
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    before = sorted(p for p in rt.cfg.state_dir.rglob("*")) if rt.cfg.state_dir.exists() else []
    app_with(rt, FakeSpeaker()).post("/tts", json={"text": "Hello."}, headers=H)
    assert list(here.iterdir()) == []
    after = sorted(p for p in rt.cfg.state_dir.rglob("*")) if rt.cfg.state_dir.exists() else []
    assert after == before


# ---- the OpenAI speaker --------------------------------------------------------------------------------


def test_speaker_reads_its_settings_from_the_environment(monkeypatch):
    for name in ("CHIEF_TTS_API_KEY", "CHIEF_TTS_BASE_URL", "CHIEF_TTS_MODEL", "CHIEF_TTS_VOICE"):
        monkeypatch.delenv(name, raising=False)
    s = OpenAISpeaker.from_env()
    assert not s.enabled and s.base_url == "https://api.openai.com/v1" and s.model == "gpt-4o-mini-tts" and s.voice == "marin"
    monkeypatch.setenv("CHIEF_TTS_API_KEY", "sk-test")
    monkeypatch.setenv("CHIEF_TTS_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("CHIEF_TTS_MODEL", "gpt-4o-mini-tts-2025-12-15")
    monkeypatch.setenv("CHIEF_TTS_VOICE", "coral")
    s = OpenAISpeaker.from_env()
    assert s.enabled and s.base_url == "https://example.test/v1" and s.model.endswith("2025-12-15") and s.voice == "coral"


def test_speaker_sends_the_documented_request(monkeypatch):
    sent = {}

    class Resp:
        content = MP3

        def raise_for_status(self):
            pass

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json, timeout=timeout)
        return Resp()

    monkeypatch.setattr(tts.httpx, "post", post)
    s = OpenAISpeaker("https://api.openai.com/v1", "sk-test", "gpt-4o-mini-tts", "marin")
    assert s.speak("Thank you!", "happy") == MP3
    assert sent["url"] == "https://api.openai.com/v1/audio/speech" and sent["headers"] == {"Authorization": "Bearer sk-test"}
    body = sent["json"]
    assert body["model"] == "gpt-4o-mini-tts" and body["input"] == "Thank you!" and body["voice"] == "marin"
    assert body["response_format"] == "mp3" and "warm" in body["instructions"] and "bright" in body["instructions"]
    assert len(body["instructions"]) <= 4096
    assert "instructions" not in OpenAISpeaker("u", "k", "tts-1", "alloy").request("Hi.", "calm")  # tts-1 does not take them


def test_each_mood_has_its_own_feeling():
    calm, happy, worried = (tts.style(m) for m in ("calm", "happy", "worried"))
    assert len({calm, happy, worried}) == 3 and all("grateful" in s for s in (calm, happy, worried))
    assert tts.style("anything else") == calm


# ---- the pages ---------------------------------------------------------------------------------------------


def test_the_voice_script_is_served_with_the_token_only(rt):
    client = app_with(rt, FakeSpeaker())
    assert client.get("/dashboard/voice.js").status_code == 401
    r = client.get("/dashboard/voice.js", headers=H)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and "ChiefVoice" in r.text
    script = VOICE_SCRIPT.read_text("utf-8")
    assert "http" not in script and 'fetch("/tts"' in script  # it talks only to Chief's own server


def test_the_voice_script_prefers_natural_voices_and_remembers_the_pick():
    script = VOICE_SCRIPT.read_text("utf-8")
    assert "natural|neural" in script and "online" in script and "google (uk english female" in script and "samantha" in script
    assert "chief-voice-name" in script and "localStorage.setItem(KEY" in script
    for mood in ("calm:", "happy:", "worried:"):
        assert mood in script
    assert "createAnalyser" in script and "decodeAudioData" in script and "AbortController" in script


def test_both_pages_pick_a_voice_fall_back_and_keep_the_mouth_moving():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert 'id="voicePick"' in page and 'id="voiceTest"' in page and 'id="voiceMore"' in page
    assert "ChiefVoice.setNatural(!!s.tts)" in page and "naturalSpeech(text, run, reveal, then, next)" in page
    assert "pet.loud(level)" in page and "pet.word(" in page and "onboundary" in page  # loudness, or words as before
    assert "() => { if (run === speechRun) fallback(); }" in page  # any problem: the browser's voice
    assert "loud(level)" in PET_SCRIPT.read_text("utf-8")
    phone = PAGE.read_text("utf-8")
    assert 'id="voicePick"' in phone and "ChiefVoice.say(text, mood" in phone and "speechSynthesis.speak(u)" in phone


# ---- warmer words --------------------------------------------------------------------------------------------


def test_the_greeting_is_warm_and_still_says_the_same_things():
    mail = {"reply": [{"from": "Sam"}], "deadline": [], "fyi": 0, "flagged": 0}
    say = opening(8, [{"title": "Standup", "clock": "10:00 AM", "end": 10.5}], mail, [], [])
    assert say.startswith("Hi, good morning! I'm glad you're here.")
    assert "starting with Standup at 10:00 AM" in say and "Sam is waiting to hear from you." in say
    assert "I'm sorry, I can't reach your calendar" in opening(20, [], mail, [], ["calendar"])


def test_done_skipped_and_undone_sound_human(rt):
    assert results_text([{"tool": "x", "result": {}}] * 2) == "All done. I made 2 changes. Thanks for looking it over first."
    assert results_text([]) == "All done. Nothing needed changing."
    handle_utterance(rt, "how's my week")
    assert handle_utterance(rt, "skip")["say"].startswith("No problem, I've skipped that one.")


def test_small_talk_is_kind():
    from chief.intent import _small_talk

    assert "thank you for letting me help" in _small_talk("thank you")
    assert _small_talk("bye").startswith("Bye for now, and thanks.")
    assert _small_talk("hi").startswith("Hi! It's good to hear from you.")
