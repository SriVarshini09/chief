from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from chief import doctor
from chief.config import Config
from chief.doctor import Deps, blocking, format_report, run_checks
from chief.tools.replay import DemoCache

NOW = datetime(2026, 10, 21, 8, 0)
SECRETS = {
    "COMPOSIO_API_KEY": "ak_SECRETCOMPOSIO1234567890",
    "PARALLEL_API_KEY": "par_SECRETPARALLEL1234567890",
    "CHIEF_LLM_API_KEY": "sk-SECRETLLM1234567890abcdef",
    "CHIEF_STT_API_KEY": "gsk_SECRETSTT1234567890abcdef",
    "CHIEF_WEB_TOKEN": "SECRETTOKEN1234567890",
}


def fake(env=None, **over) -> Deps:
    d = Deps(
        env=dict(env or {}),
        version=(3, 13, 1),
        in_venv=lambda: True,
        has_module=lambda name: True,
        which=lambda name: f"/usr/bin/{name}",
        port_free=lambda port: True,
        find_browser=lambda explicit: "/opt/chrome",
        exists=lambda p: False,
        now=lambda: NOW,
        composio_status=lambda: {"gmail": True, "googlecalendar": True},
        live_calls={"parallel": lambda: "ok", "llm": lambda: "ok", "stt": lambda: "ok"},
        platform="linux",
    )
    for k, v in over.items():
        setattr(d, k, v)
    return d


def by_name(checks):
    return {c.name: c for c in checks}


def cfg(tmp_path, mode="demo", **kw):
    return Config(mode=mode, state_dir=tmp_path / "s", cache_file=tmp_path / "cache.json", llm_api_key="x", llm_model="m", **kw)


def test_values_of_keys_are_never_printed(tmp_path):
    env = {**SECRETS, "CHIEF_MODE": "demo"}
    checks = run_checks(cfg(tmp_path), fake(env), live=True)
    text = format_report(checks)
    for secret in SECRETS.values():
        assert secret not in text
    assert "Env COMPOSIO_API_KEY: set" in text and "Env PARALLEL_API_KEY: set" in text


def test_missing_keys_are_critical_in_demo_mode_with_a_fix_hint(tmp_path):
    checks = run_checks(cfg(tmp_path), fake({}))
    got = by_name(checks)
    for name in ("Env COMPOSIO_API_KEY", "Env PARALLEL_API_KEY"):
        assert got[name].status == "fail" and got[name].critical and ".env" in got[name].fix
    assert got["Composio connections"].blocking and "COMPOSIO_API_KEY" in got["Composio connections"].detail
    assert blocking(checks)
    assert "critical problem(s) must be fixed" in format_report(checks)


def test_optional_things_only_warn(tmp_path):
    env = {k: v for k, v in SECRETS.items() if k not in ("CHIEF_STT_API_KEY", "CHIEF_LLM_API_KEY")}
    checks = run_checks(cfg(tmp_path), fake(env, which=lambda n: None))
    got = by_name(checks)
    assert got["Env CHIEF_STT_API_KEY"].status == "warn" and not got["Env CHIEF_STT_API_KEY"].critical
    assert got["cloudflared (phone tunnel)"].status == "warn" and "winget install" in got["cloudflared (phone tunnel)"].fix
    assert got["Demo cache"].status == "warn" and "record-demo" in got["Demo cache"].fix
    assert not blocking(checks)


def test_everything_green_exits_zero(tmp_path):
    path = tmp_path / "cache.json"
    cache = DemoCache(path)
    cache.put("fetch_emails", {"query": "", "max_results": 10}, [])
    cache.save()
    d = fake({**SECRETS}, exists=lambda p: Path(p) == path)
    out: list[str] = []
    code = doctor.doctor_main(cfg(tmp_path), deps=d, live=True, out=out.append)
    text = "\n".join(out)
    assert code == 0 and "[FAIL]" not in text and "All critical checks passed." in text
    assert "Demo cache: 1 entries (1 fetch_emails)" in text


def test_cache_age_is_reported_and_stale_caches_warn(tmp_path):
    path = tmp_path / "cache.json"
    cache = DemoCache(path)
    cache.put("read_health", {"days": 14}, [])
    cache.save()
    d = fake(SECRETS, exists=lambda p: Path(p) == path, now=lambda: datetime.now() + timedelta(hours=30))
    c = by_name(run_checks(cfg(tmp_path), d))["Demo cache"]
    assert c.status == "warn" and "recorded 30.0 h ago" in c.detail and "record-demo" in c.fix


def test_offline_demo_needs_the_cache(tmp_path):
    checks = run_checks(cfg(tmp_path, mode="demo-offline"), fake({}))
    cache = by_name(checks)["Demo cache"]
    assert cache.blocking
    assert "Env COMPOSIO_API_KEY" in by_name(checks) and not by_name(checks)["Env COMPOSIO_API_KEY"].critical  # no keys needed offline
    assert "Composio connections" not in by_name(checks)


def test_composio_connection_status(tmp_path):
    d = fake(SECRETS, composio_status=lambda: {"gmail": True, "googlecalendar": False})
    c = by_name(run_checks(cfg(tmp_path), d))["Composio connections"]
    assert c.blocking and "googlecalendar" in c.detail and "composio_client" in c.fix

    def boom():
        raise ConnectionError("no route to host")

    c = by_name(run_checks(cfg(tmp_path), fake(SECRETS, composio_status=boom)))["Composio connections"]
    assert c.blocking and "ConnectionError" in c.detail
    c = by_name(run_checks(cfg(tmp_path), fake(SECRETS), offline=True))["Composio connections"]
    assert c.status == "info" and "skipped" in c.detail


def test_live_calls_are_optional_timed_and_reported(tmp_path, monkeypatch):
    names = [c.name for c in run_checks(cfg(tmp_path), fake(SECRETS))]
    assert not any(n.startswith("Live call") for n in names)

    monkeypatch.setattr(doctor, "NET_TIMEOUT", 0.2)

    def hang():
        import time

        time.sleep(2)

    def rejected():
        raise ValueError("401")

    calls = {"parallel": lambda: "search returned 1 result(s)", "llm": hang, "stt": rejected}
    got = by_name(run_checks(cfg(tmp_path), fake(SECRETS, live_calls=calls), live=True))
    assert got["Live call: Parallel search"].status == "ok"
    assert got["Live call: Speech-to-text host"].status == "warn" and "ValueError" in got["Live call: Speech-to-text host"].detail
    assert got["Live call: LLM host"].status == "warn" and "no answer within 0.2s" in got["Live call: LLM host"].detail


def test_busy_port_and_old_python_block(tmp_path):
    checks = run_checks(cfg(tmp_path, mode="mock"), fake({}, port_free=lambda p: False, version=(3, 9, 0)), port=9999)
    got = by_name(checks)
    assert got["Port 9999"].blocking and "already in use" in got["Port 9999"].detail
    assert got["Python version"].blocking and "3.11" in got["Python version"].fix


def test_missing_packages_and_browser_and_venv(tmp_path):
    missing = {"composio", "qrcode", "segno"}
    d = fake(SECRETS, has_module=lambda n: n not in missing, in_venv=lambda: False,
             find_browser=lambda e: (_ for _ in ()).throw(RuntimeError("no Chrome or Edge found")))
    got = by_name(run_checks(cfg(tmp_path, show_windows=True), d))
    assert got["Package composio"].blocking and "pip install" in got["Package composio"].fix
    assert got["Virtual environment"].status == "warn"
    assert got["Browser for real windows"].status == "fail" and "CHIEF_SHOW_BROWSER" in got["Browser for real windows"].fix
    assert "optional" in got["QR code package"].detail


def test_mock_mode_is_quiet(tmp_path):
    checks = run_checks(cfg(tmp_path, mode="mock"), fake({}, has_module=lambda n: n in ("fastapi", "uvicorn", "multipart", "httpx")))
    assert not blocking(checks)
    assert {c.name for c in checks if c.status == "warn"} <= {"cloudflared (phone tunnel)"}  # a laptop with nothing live set up is not nagged
    assert by_name(checks)["Package composio"].status == "info"
