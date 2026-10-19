from __future__ import annotations

from datetime import timedelta

from chief.intent import handle_utterance
from chief.runtime import Runtime


def test_prep_builds_a_brief_with_history_and_sources(rt):
    p = rt.run_skill("meeting_prep")
    assert p is not None and p.actions == []
    assert "Acme pricing review" in p.summary and "Dana" in p.summary and "Acme" in p.summary
    assert "Pricing follow-up" in p.summary and "kickoff notes" in p.summary  # past threads
    assert p.sources == ["https://example.com/mock/acme"]
    assert "https://example.com/mock/acme" in p.summary
    assert "MOCK" in p.summary  # the mock web result stays labelled


def test_prep_sends_only_the_company_name_to_the_web(rt, tools):
    seen = []
    real = tools.search_web

    def spy(objective, queries, max_results=5):
        seen.append(objective + " " + " ".join(queries))
        return real(objective, queries, max_results)

    tools.search_web = spy
    rt.run_skill("meeting_prep")
    assert seen and all("dana" not in s.lower() and "@" not in s for s in seen)


def test_prep_by_voice_is_read_only(rt, tools):
    out = handle_utterance(rt, "prep me for my next meeting")
    assert out["proposal"].skill == "meeting_prep"
    assert tools.calls == []


def test_prep_silent_without_an_external_meeting(rt, tools):
    del tools.events["ev-ext-acme"]  # what remains is internal, or a bare name with no address
    assert rt.run_skill("meeting_prep") is None


def test_prep_ignores_meetings_beyond_the_window(cfg, tools, cards):
    rt = Runtime(cfg, tools=tools, notify=cards.append)
    assert rt.run_skill("meeting_prep", now=cfg.now + timedelta(days=3)) is None


def test_prep_silent_with_no_history_and_no_web(rt, tools):
    tools.fetch_emails = lambda query="", max_results=10: []
    tools.search_web = lambda objective, queries, max_results=5: []
    assert rt.run_skill("meeting_prep") is None


def test_prep_still_useful_with_only_mail(rt, tools):
    tools.search_web = lambda objective, queries, max_results=5: []
    p = rt.run_skill("meeting_prep")
    assert p is not None and p.sources == [] and "Past threads" in p.summary
