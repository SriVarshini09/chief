from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from chief.intent import handle_utterance
from chief.models import Event
from chief.runtime import Runtime
from chief.skills.trip_planner import parse_request

ASK = "I need to head to Palo Alto after this, find me a Caltrain after 6 and a boba place on the way"
TRAIN_URL = "https://www.caltrain.com/schedules"
MOCK_TRAIN = "https://example.com/mock/caltrain"


def stub_train(tools, excerpts, url=TRAIN_URL):
    """Replace only the train search; boba still comes from the mock."""
    real = tools.search_web

    def search(objective, queries, max_results=5):
        if "boba" in (objective + " ".join(queries)).lower():
            return real(objective, queries, max_results)
        return [{"title": "Caltrain schedule", "url": url, "excerpts": excerpts}] if excerpts is not None else []

    tools.search_web = search


class FakeLLM:
    enabled = True

    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, fallback):
        return self.reply


def test_parse_request():
    assert parse_request(ASK, None) == ("Palo Alto", time(18, 0), True)
    assert parse_request("head to San Jose after 7:30pm", None)[:2] == ("San Jose", time(19, 30))
    assert parse_request("I'm heading to Mountain View tomorrow", None)[:2] == ("Mountain View", None)
    assert parse_request("what's the weather", None) is None


def test_full_ask_proposes_one_event_with_source(rt):
    out = handle_utterance(rt, ASK)
    p = out["proposal"]
    assert p.skill == "trip_planner" and [a.tool for a in p.actions] == ["create_event"]
    ev = p.actions[0].args
    assert ev["title"] == "Caltrain to Palo Alto"
    assert ev["start"] == "2026-10-21T18:30:00" and ev["end"] == "2026-10-21T19:15:00"  # 5:30 PM is before 6
    assert MOCK_TRAIN in ev["description"] and "MOCK" in ev["description"]
    assert MOCK_TRAIN in p.summary and "6:30 PM" in p.summary
    assert "5:30" not in p.summary
    assert "https://example.com/mock/boba" in p.summary
    assert set(p.sources) == {MOCK_TRAIN, "https://example.com/mock/boba"}


def test_search_objective_pins_caltrain_and_511(rt, tools):
    objectives = []
    real = tools.search_web

    def spy(objective, queries, max_results=5):
        objectives.append(objective)
        return real(objective, queries, max_results)

    tools.search_web = spy
    rt.run_skill("trip_planner", request=ASK)
    train = next(o for o in objectives if "Caltrain departures" in o)
    assert "caltrain.com" in train and "511.org" in train


def test_nothing_is_created_until_approved(rt, tools):
    rt.run_skill("trip_planner", request=ASK)
    assert tools.calls == []


def test_approving_creates_the_calendar_event(rt, tools):
    p = rt.run_skill("trip_planner", request=ASK)
    assert "error" not in rt.gate.approve(p.id)[0]
    assert any(e.title == "Caltrain to Palo Alto" for e in tools.events.values())


def test_a_calendar_conflict_moves_to_the_next_train(rt, tools):
    busy = datetime(2026, 10, 21, 18, 20)
    tools.events["ev-busy"] = Event("ev-busy", "Dinner", busy, busy + timedelta(minutes=70))
    p = rt.run_skill("trip_planner", request=ASK)
    assert p.actions[0].args["start"] == "2026-10-21T19:30:00"


# --- never invents times
@pytest.mark.parametrize(
    "excerpts",
    [
        ["Caltrain runs frequently in the evening. See the timetable for details."],  # no times at all
        [],
        ["Northbound departs 4:10 PM arrives 4:55 PM."],  # only before the requested time
        None,  # search returns nothing
    ],
)
def test_no_times_found_says_so_and_proposes_nothing(rt, tools, excerpts):
    stub_train(tools, excerpts)
    p = rt.run_skill("trip_planner", request=ASK)
    assert p.actions == []
    assert "couldn't find" in p.summary and "not guessing" in p.summary
    assert p.key.endswith(":none")
    assert rt.gate.pending == {}  # nothing to approve, so nothing is queued
    assert tools.calls == []


def test_times_from_an_untrusted_site_are_ignored(rt, tools):
    stub_train(tools, ["Departs 7:00 PM arrives 7:45 PM."], url="https://spam.example/trains")
    p = rt.run_skill("trip_planner", request=ASK)
    assert p.actions == []


def test_source_times_are_used_exactly(rt, tools):
    stub_train(tools, ["Northbound train leaves 6:42 PM and arrives 7:20 PM."])
    p = rt.run_skill("trip_planner", request=ASK)
    ev = p.actions[0].args
    assert ev["start"] == "2026-10-21T18:42:00" and ev["end"] == "2026-10-21T19:20:00"
    assert TRAIN_URL in p.summary and TRAIN_URL in ev["description"] and TRAIN_URL in p.sources


def test_missing_arrival_is_not_made_up(rt, tools):
    stub_train(tools, ["Evening service leaves 6:42 PM."])
    p = rt.run_skill("trip_planner", request=ASK)
    assert "arrives" not in p.summary and "placeholder" in p.summary


def test_model_cannot_introduce_a_time_or_drop_the_source(cfg, tools, cards):
    rt = Runtime(cfg, tools=tools, notify=cards.append, llm=FakeLLM("Take the 7:45 PM train, it is quick."))
    p = rt.run_skill("trip_planner", request=ASK)
    assert "7:45" not in p.summary and "6:30 PM" in p.summary and MOCK_TRAIN in p.summary


def test_model_rephrase_that_keeps_the_facts_is_used(cfg, tools, cards):
    reply = f"Train at 6:30 PM, arrives 7:15 PM ({MOCK_TRAIN}). Approve?"
    rt = Runtime(cfg, tools=tools, notify=cards.append, llm=FakeLLM(reply))
    assert rt.run_skill("trip_planner", request=ASK).summary == reply


# --- silence
@pytest.mark.parametrize("request_text", ["", "   ", "what's the weather like", "how's my inbox"])
def test_silent_without_a_trip_request(rt, tools, request_text):
    assert rt.run_skill("trip_planner", request=request_text) is None
    assert tools.calls == []


def test_a_heartbeat_run_is_silent(rt):
    assert rt.run_skill("trip_planner") is None
