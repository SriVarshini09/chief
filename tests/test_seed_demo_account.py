"""scripts/seed_demo_account.py against a fake IMAP class and a fake tools object. No network, no accounts."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timedelta
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

from chief import health
from chief.models import Email, Event
from chief.skills.email_brief import classify
from chief.tools.mock import MockTools

_SPEC = importlib.util.spec_from_file_location("seed_demo_account", Path(__file__).resolve().parent.parent / "scripts" / "seed_demo_account.py")
seed = importlib.util.module_from_spec(_SPEC)
sys.modules["seed_demo_account"] = seed
_SPEC.loader.exec_module(seed)

NOW = datetime(2026, 10, 21, 8, 0)  # a Wednesday
ADDRESS = "chief.demo.test@gmail.com"
PASSWORD = "abcd efgh ijkl mnop"
SECRET = "abcdefghijklmnop"
ENV = {seed.ENV_GMAIL: ADDRESS, seed.ENV_PASSWORD: PASSWORD}
APPLY = ["--apply", seed.DEMO_FLAG]


class FakeIMAP:
    """Records every call. Anything outside login/select/search/append/logout fails the test."""

    instances: list["FakeIMAP"] = []
    ALLOWED = {"login", "select", "search", "append", "logout"}

    def __init__(self, host, preload=()):
        self.host, self.calls, self.appended = host, [], []
        self.ids = set(preload)
        FakeIMAP.instances.append(self)

    def __getattr__(self, name):  # store, expunge, delete, copy ... all land here
        raise AssertionError(f"forbidden IMAP call: {name}")

    def _log(self, name, *args):
        self.calls.append((name, args))

    def login(self, user, password):
        self._log("login", user)
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self._log("select", mailbox)
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self._log("search", *criteria)
        return "OK", [b"1" if criteria[-1] in self.ids else b""]

    def append(self, mailbox, flags, date_time, raw):
        self._log("append", mailbox, flags)
        self.appended.append((mailbox, flags, date_time, raw))
        self.ids.add(message_from_bytes(raw)["Message-ID"])
        return "OK", [b"done"]

    def logout(self):
        self._log("logout")
        return "BYE", [b""]


class FakeTools:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.created: list[dict] = []
        self.calls: list[str] = []

    def list_events(self, start, end):
        self.calls.append("list_events")
        return list(self.existing)

    def create_event(self, title, start, end, description=""):
        self.calls.append("create_event")
        self.created.append({"title": title, "start": start, "end": end, "description": description})
        return {"id": f"x{len(self.created)}"}

    def __getattr__(self, name):  # delete_event, patch_event, send_email ... must never be reached
        raise AssertionError(f"forbidden tools call: {name}")


class Run:
    def __init__(self, tmp_path, argv, env=ENV, answer=ADDRESS, imap=None, tools=None):
        self.lines: list[str] = []
        self.imap = imap or (lambda host: FakeIMAP(host))
        self.tools = tools or FakeTools()
        self.prompts: list[str] = []

        def ask(prompt):
            self.prompts.append(prompt)
            return answer

        FakeIMAP.instances.clear()
        self.code = seed.main(
            argv, env=env, now=NOW, imap_factory=self.imap, tools_factory=lambda: self.tools,
            input_fn=ask, write=self.lines.append, csv_path=tmp_path / "data" / "sample_health.csv",
        )
        self.text = "\n".join(self.lines)


# ------------------------------------------------------------------------------------------ dry run
def test_dry_run_is_the_default_and_touches_nothing(tmp_path):
    r = Run(tmp_path, [], env={})
    assert r.code == 0
    assert "DRY RUN" in r.text
    assert not FakeIMAP.instances  # no connection opened
    assert not r.tools.calls  # Composio never touched
    assert not (tmp_path / "data").exists()  # no CSV written
    assert not r.prompts  # no confirmation needed


def test_dry_run_prints_the_plan(tmp_path):
    r = Run(tmp_path, [], env={})
    for subject in ("Deck for Friday?", "Pricing follow-up", "Invoice 1042", "Weekly digest", "Acme rollout: kickoff notes", "Acme security questionnaire"):
        assert f"Subject: {subject}" in r.text
    for title in ("Standup", "Product review", "1:1 with Sam", "Design crit", "Vendor sync", "Weekly status", "Acme pricing review"):
        assert title in r.text
    assert "Message-ID: <chief-demo-seed-sam-deck@" in r.text
    assert seed.MARKER in r.text
    assert "SAMPLE" in r.text


def test_apply_without_flag_refuses(tmp_path):
    r = Run(tmp_path, ["--apply"])
    assert r.code == 2 and seed.DEMO_FLAG in r.text
    assert not FakeIMAP.instances and not r.prompts and not r.tools.calls


@pytest.mark.parametrize("address", ["jane.doe@gmail.com", "demo@corp.example", "demo.user@yahoo.com"])
def test_non_demo_looking_address_is_refused_even_in_a_dry_run(tmp_path, address):
    for argv in ([], APPLY):
        r = Run(tmp_path, argv, env={seed.ENV_GMAIL: address, seed.ENV_PASSWORD: PASSWORD})
        assert r.code == 2 and "does not look like the demo account" in r.text
        assert not FakeIMAP.instances and not r.prompts and not r.tools.calls


def test_apply_needs_address_and_password(tmp_path):
    assert Run(tmp_path, APPLY, env={seed.ENV_PASSWORD: PASSWORD}).code == 2
    assert Run(tmp_path, APPLY, env={seed.ENV_GMAIL: ADDRESS}).code == 2
    assert not FakeIMAP.instances


def test_password_is_not_a_command_line_option(tmp_path):
    with pytest.raises(SystemExit):
        seed.main(["--password", "x"], env={}, write=lambda s: None)


# ------------------------------------------------------------------------------------------ confirmation
@pytest.mark.parametrize("answer", ["", "yes", "y", "someone.else@gmail.com"])
def test_wrong_confirmation_cancels_everything(tmp_path, answer):
    r = Run(tmp_path, APPLY, answer=answer)
    assert r.code == 1 and "Cancelled" in r.text
    assert r.prompts  # it did ask
    assert not FakeIMAP.instances and not r.tools.created
    assert not (tmp_path / "data").exists()


def test_eof_on_confirmation_cancels(tmp_path):
    def boom(prompt):
        raise EOFError

    code = seed.main(APPLY, env=ENV, now=NOW, imap_factory=lambda h: FakeIMAP(h), tools_factory=FakeTools, input_fn=boom, write=lambda s: None)
    assert code == 1


def test_typed_address_applies(tmp_path):
    r = Run(tmp_path, APPLY)
    assert r.code == 0
    assert len(FakeIMAP.instances[0].appended) == 10
    assert r.tools.created
    assert (tmp_path / "data" / "sample_health.csv").exists()
    assert "SAMPLE" in r.text


# ------------------------------------------------------------------------------------------ secrets
def test_password_never_appears_in_output(tmp_path):
    for argv in ([], APPLY):
        r = Run(tmp_path, argv)
        assert SECRET not in r.text and PASSWORD not in r.text


def test_password_is_scrubbed_even_if_something_prints_it(tmp_path):
    lines: list[str] = []
    out = seed.Out(lines.append, secrets=(SECRET,))
    out(f"login failed for {SECRET}")
    assert SECRET not in lines[0] and "***" in lines[0]


def test_failed_login_does_not_leak_password(tmp_path):
    import imaplib

    class Bad(FakeIMAP):
        def login(self, user, password):
            raise imaplib.IMAP4.error(f"AUTHENTICATIONFAILED {password}")

    r = Run(tmp_path, APPLY + ["--only", "mail"], imap=lambda h: Bad(h))
    assert r.code == 1 and SECRET not in r.text and PASSWORD not in r.text


def test_login_receives_password_without_spaces(tmp_path):
    seen = []

    class Spy(FakeIMAP):
        def login(self, user, password):
            seen.append((user, password))
            return super().login(user, password)

    Run(tmp_path, APPLY + ["--only", "mail"], imap=lambda h: Spy(h))
    assert seen == [(ADDRESS, SECRET)]


# ------------------------------------------------------------------------------------------ mail content
def test_emails_have_headers_and_relative_dates():
    mails = seed.build_emails(NOW)
    assert 9 <= len(mails) <= 11
    for m in mails:
        msg = message_from_bytes(m.to_message(ADDRESS).as_bytes())
        for h in ("From", "To", "Subject", "Date", "Message-ID"):
            assert msg[h], h
        assert msg["To"] == ADDRESS
        assert re.search(r"@(example\.com|[\w.-]+\.example)>?", msg["From"]), msg["From"]  # fictional domains only
        assert parsedate_to_datetime(msg["Date"]) <= NOW.astimezone()

    later = datetime(2027, 3, 3, 8, 0)  # run on another day: every date moves with it
    for a, b in zip(seed.build_emails(NOW), seed.build_emails(later)):
        assert b.date.replace(tzinfo=None) - a.date.replace(tzinfo=None) == later - NOW  # wall clock (DST-proof)
        assert a.message_id == b.message_id  # stable ids keep re-runs idempotent across days


def test_emails_match_the_story_and_classify_as_expected():
    by = {m.key: m for m in seed.build_emails(NOW)}

    def kind(key):
        m = by[key]
        return classify(Email(key, key, m.sender, m.subject, " ".join(m.body.split()), m.date.replace(tzinfo=None))).kind

    assert kind("sam-deck") == "reply" and "by Friday" in by["sam-deck"].body
    assert kind("dana-pricing") == "reply"
    assert kind("invoice-1042") == "deadline"
    assert kind("newsletter-digest") == "fyi"
    assert kind("maya-roadmap") == "reply"
    assert kind("dana-kickoff") and kind("dana-security")
    assert sum(1 for k in by if kind(k) == "suspicious") == 1  # exactly one injection attempt
    assert kind("injection") == "suspicious"
    assert sum("acme.example" in m.sender for m in by.values()) >= 3  # Dana: pricing + two older threads


def test_invoice_due_date_is_relative():
    inv = {m.key: m for m in seed.build_emails(NOW)}["invoice-1042"]
    assert "due Nov 1" in inv.body  # NOW + 11 days
    later = {m.key: m for m in seed.build_emails(NOW + timedelta(days=30))}["invoice-1042"]
    assert "due Dec 1" in later.body


def test_messages_are_appended_unread_to_inbox(tmp_path):
    Run(tmp_path, APPLY + ["--only", "mail"])
    imap = FakeIMAP.instances[0]
    assert imap.host == "imap.gmail.com"
    assert len(imap.appended) == 10
    for mailbox, flags, date_time, raw in imap.appended:
        assert mailbox == "INBOX"
        assert flags == ""  # no \Seen = unread
        assert date_time.startswith('"')
        assert message_from_bytes(raw)["X-Chief-Demo-Seed"] == "1"


def test_imap_only_ever_reads_and_appends(tmp_path):
    Run(tmp_path, APPLY)
    names = {c[0] for imap in FakeIMAP.instances for c in imap.calls}
    assert names <= FakeIMAP.ALLOWED  # a store/expunge/delete/copy call would also have raised in the fake
    assert "append" in names


def test_second_run_adds_no_duplicate_mail(tmp_path):
    first = Run(tmp_path, APPLY + ["--only", "mail"])
    ids = FakeIMAP.instances[0].ids
    second = Run(tmp_path, APPLY + ["--only", "mail"], imap=lambda h: FakeIMAP(h, preload=ids))
    assert len(FakeIMAP.instances[0].appended) == 0
    assert second.text.count("already in INBOX") == 10
    assert first.code == second.code == 0


# ------------------------------------------------------------------------------------------ calendar
def test_events_match_the_mock_story_and_are_relative():
    events = seed.build_events(NOW)
    mock = MockTools(NOW)
    story = {(e.title, e.start) for e in mock.events.values() if e.id != "ev-ext-acme"}
    mine = {(e.title, e.start) for e in events if not e.key.startswith("ev-ext-")}
    assert mine == story
    ext = [e for e in events if e.key.startswith("ev-ext-")]
    assert len(ext) == 1 and ext[0].title == "Acme pricing review"
    assert seed.DANA in ext[0].attendees and seed.DANA.endswith(".example")
    assert ext[0].start.date() >= NOW.date()
    assert min(e.start for e in events).date() >= NOW.date() - timedelta(days=14)
    assert max(e.start for e in events).date() <= NOW.date() + timedelta(days=7)
    heavy = [d for d in {e.start.date() for e in events} if d.weekday() in health.HEAVY_WEEKDAYS]
    assert heavy and all(sum(e.start.date() == d for e in events) == 6 for d in heavy)

    shifted = seed.build_events(NOW + timedelta(days=7))  # same weekday: everything moves a week
    assert {(e.title, e.start) for e in shifted if not e.key.startswith("ev-ext-")} == {(t, s + timedelta(days=7)) for t, s in mine}


def test_every_created_event_carries_the_marker(tmp_path):
    r = Run(tmp_path, APPLY + ["--only", "calendar"])
    assert r.tools.created
    assert all(seed.MARKER in c["description"] for c in r.tools.created)
    assert len(r.tools.created) == len(seed.build_events(NOW))
    ext = next(c for c in r.tools.created if c["title"] == "Acme pricing review")
    assert "dana@acme.example" in ext["description"]
    assert set(r.tools.calls) <= {"list_events", "create_event"}  # nothing deleted or patched


def test_second_run_adds_no_duplicate_events(tmp_path):
    first = Run(tmp_path, APPLY + ["--only", "calendar"])
    existing = [
        Event(f"id{i}", c["title"], datetime.fromisoformat(c["start"]), datetime.fromisoformat(c["end"]), c["description"])
        for i, c in enumerate(first.tools.created)
    ]
    second = Run(tmp_path, APPLY + ["--only", "calendar"], tools=FakeTools(existing))
    assert second.tools.created == []
    assert second.code == 0


def test_existing_event_at_the_same_time_is_never_duplicated_or_touched(tmp_path):
    mine = seed.build_events(NOW)[0]
    clash = Event("real-1", mine.title, mine.start, mine.end, "someone else's event")
    r = Run(tmp_path, APPLY + ["--only", "calendar"], tools=FakeTools([clash]))
    assert len(r.tools.created) == len(seed.build_events(NOW)) - 1
    assert all(c["start"] != mine.start.isoformat() or c["title"] != mine.title for c in r.tools.created)


def test_calendar_failure_is_reported_not_hidden(tmp_path):
    class Flaky(FakeTools):
        def create_event(self, *a, **k):
            raise RuntimeError("boom")

    r = Run(tmp_path, APPLY + ["--only", "calendar"], tools=Flaky())
    assert r.code == 1 and "FAILED" in r.text


# ------------------------------------------------------------------------------------------ sleep csv
def test_csv_is_sample_data_anchored_to_today(tmp_path):
    r = Run(tmp_path, APPLY + ["--only", "sleep"])
    path = tmp_path / "data" / "sample_health.csv"
    rows = health.read_csv(path)
    assert rows[-1].day == NOW.date()
    assert path.read_text(encoding="utf-8").splitlines()[0] == seed.CSV_HEADER
    assert "SAMPLE" in r.text and not FakeIMAP.instances and not r.tools.calls


def test_csv_that_is_not_a_sample_file_is_left_alone(tmp_path):
    path = tmp_path / "data" / "sample_health.csv"
    path.parent.mkdir()
    path.write_text("my,real,export\n1,2,3\n", encoding="utf-8")
    Run(tmp_path, APPLY + ["--only", "sleep"])
    assert path.read_text(encoding="utf-8") == "my,real,export\n1,2,3\n"


# ------------------------------------------------------------------------------------------ hygiene
def test_script_has_no_delete_or_modify_calls():
    src = (Path(seed.__file__)).read_text(encoding="utf-8")
    for forbidden in (".store(", ".expunge(", ".delete(", ".copy(", ".delete_event(", ".patch_event(", ".send_email(", "sys.argv"):
        assert forbidden not in src, forbidden
    assert "print(password" not in src.lower()


def test_env_example_lists_the_demo_variables_empty():
    text = (Path(seed.__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^CHIEF_DEMO_GMAIL=$", text, re.M)
    assert re.search(r"^CHIEF_DEMO_APP_PASSWORD=$", text, re.M)
