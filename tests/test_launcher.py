from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from chief import launcher
from chief.config import Config
from chief.doctor import Deps
from chief.tools.mock import MockTools
from chief.tools.replay import STORY, DemoCache

NOW = datetime(2026, 10, 21, 8, 0)
TUNNEL_LINES = [
    "2026-10-21T08:00:00Z INF Thank you for trying Cloudflare Tunnel.\n",
    "2026-10-21T08:00:02Z INF |  https://quiet-river-demo.trycloudflare.com  |\n",
    "2026-10-21T08:00:03Z INF Registered tunnel connection\n",
]


class FakeProc:
    def __init__(self, lines=TUNNEL_LINES):
        self.stdout = iter(lines)
        self.terminated = self.killed = False
        self.args = None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def deps(*, cloudflared=True, port_free=True, env=None):
    return Deps(
        env=dict(env or {}),
        version=(3, 13, 1),
        in_venv=lambda: True,
        has_module=lambda n: True,
        which=lambda n: "/bin/cloudflared" if (cloudflared and n == "cloudflared") else None,
        port_free=lambda p: port_free,
        find_browser=lambda e: "/opt/chrome",
        exists=lambda p: False,
        now=lambda: NOW,
        composio_status=lambda: {"gmail": True, "googlecalendar": True},
        platform="linux",
    )


def run(tmp_path, monkeypatch, *, mode="mock", token_env=None, qr=None, **kw):
    """Run the launcher with a fake server and a fake cloudflared. Returns (exit code, output lines, calls)."""
    if token_env is None:
        monkeypatch.delenv("CHIEF_WEB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CHIEF_WEB_TOKEN", token_env)
    cfg = Config(mode="mock", state_dir=tmp_path / "s", now=NOW)
    cfg.mode = mode
    lines: list[str] = []
    calls = SimpleNamespace(served=None, proc=None, popen_args=None, runtime=None)

    def serve(rt, token, host, port):
        calls.served = (rt, token, host, port)

    def popen(args, **kwargs):
        calls.popen_args, calls.proc = args, FakeProc()
        return calls.proc

    def make_runtime(c):
        calls.runtime = SimpleNamespace(cfg=c, inner_tools=SimpleNamespace(closed=False, close=lambda: setattr(calls, "closed", True)))
        return calls.runtime

    code = launcher.run_demo(
        cfg, deps=kw.pop("deps", None) or deps(), make_runtime=make_runtime, serve_fn=serve, popen=popen, out=lines.append,
        qr=qr or (lambda url, out: False), offline_check=True, **kw,
    )
    return code, "\n".join(lines), calls


def test_demo_prints_urls_phrases_and_serves_with_a_generated_token(tmp_path, monkeypatch):
    code, text, calls = run(tmp_path, monkeypatch, port=9123)
    assert code == 0
    _, token, host, port = calls.served
    assert len(token) >= 16 and host == "127.0.0.1" and port == 9123
    assert f"http://localhost:9123/stage?token={token}" in text
    assert f"http://localhost:9123/?token={token}" in text
    assert "not set, so this token is random" in text
    for phrase, _ in STORY:
        assert phrase in text


def test_demo_uses_the_configured_token_and_never_prints_other_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "ak_SECRETVALUE1234567890")
    code, text, calls = run(tmp_path, monkeypatch, token_env="my-fixed-token-1234567890")
    assert calls.served[1] == "my-fixed-token-1234567890" and "random" not in text
    assert "ak_SECRETVALUE" not in text


def test_tunnel_is_started_read_for_the_phone_link_and_stopped_cleanly(tmp_path, monkeypatch):
    seen = []
    code, text, calls = run(tmp_path, monkeypatch, qr=lambda url, out: seen.append(url) or True)
    assert calls.popen_args == ["/bin/cloudflared", "tunnel", "--url", "http://localhost:8765"]
    token = calls.served[1]
    assert f"https://quiet-river-demo.trycloudflare.com/?token={token}" in text
    assert seen == [f"https://quiet-river-demo.trycloudflare.com/?token={token}"]  # QR got the phone link
    assert calls.proc.terminated and not calls.proc.killed
    assert calls.closed  # the failover thread pool is shut down too


def test_tunnel_is_stopped_even_if_the_server_crashes(tmp_path, monkeypatch):
    cfg = Config(mode="mock", state_dir=tmp_path / "s", now=NOW)
    procs = []

    def popen(args, **kw):
        procs.append(FakeProc())
        return procs[0]

    def serve(*a):
        raise RuntimeError("port exploded")

    with pytest.raises(RuntimeError):
        launcher.run_demo(cfg, deps=deps(), make_runtime=lambda c: SimpleNamespace(), serve_fn=serve, popen=popen, out=lambda s: None, offline_check=True)
    assert procs[0].terminated


def test_ctrl_c_is_a_clean_exit(tmp_path, monkeypatch):
    cfg = Config(mode="mock", state_dir=tmp_path / "s", now=NOW)
    procs = []

    def serve(*a):
        raise KeyboardInterrupt

    def popen(args, **kw):
        procs.append(FakeProc())
        return procs[0]

    code = launcher.run_demo(cfg, deps=deps(), make_runtime=lambda c: SimpleNamespace(), serve_fn=serve, popen=popen, out=lambda s: None, offline_check=True)
    assert code == 0 and procs[0].terminated


def test_no_cloudflared_means_a_hint_and_no_install(tmp_path, monkeypatch):
    code, text, calls = run(tmp_path, monkeypatch, deps=deps(cloudflared=False))
    assert code == 0 and calls.proc is None and calls.popen_args is None
    assert "cloudflared is not installed" in text and "winget install --id Cloudflare.cloudflared" in text


def test_no_tunnel_flag(tmp_path, monkeypatch):
    code, text, calls = run(tmp_path, monkeypatch, use_tunnel=False)
    assert calls.popen_args is None and "Tunnel: off" in text


def test_a_failing_critical_check_stops_the_launch(tmp_path, monkeypatch):
    code, text, calls = run(tmp_path, monkeypatch, deps=deps(port_free=False))
    assert code == 1 and calls.served is None and calls.runtime is None
    assert "Port 8765: already in use" in text and "Not starting" in text
    code, text, calls = run(tmp_path, monkeypatch, deps=deps(port_free=False), force=True)
    assert code == 0 and calls.served is not None and "--force given" in text


def test_demo_mode_with_keys_starts_and_explains_failover(tmp_path, monkeypatch):
    keys = {"COMPOSIO_API_KEY": "k1", "PARALLEL_API_KEY": "k2"}
    code, text, calls = run(tmp_path, monkeypatch, mode="demo", deps=deps(env=keys))
    assert code == 0 and calls.served is not None
    assert "DEMO: live, with cached answers" in text and "Wi-Fi drops" in text and "demo-offline" in text
    assert "k1" not in text and "k2" not in text


def test_demo_mode_without_keys_is_refused_with_fix_hints(tmp_path, monkeypatch):
    code, text, calls = run(tmp_path, monkeypatch, mode="demo")
    assert code == 1 and calls.served is None
    assert "COMPOSIO_API_KEY" in text and "fix:" in text


def test_offline_demo_needs_a_cache_and_then_starts(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    cfg_kwargs = dict(state_dir=tmp_path / "s", now=NOW, cache_file=cache_file)
    cfg = Config(mode="demo-offline", **cfg_kwargs)
    out: list[str] = []
    assert launcher.run_demo(cfg, deps=deps(), make_runtime=lambda c: SimpleNamespace(), serve_fn=lambda *a: None, out=out.append, offline_check=True) == 1
    assert "Demo cache" in "\n".join(out)
    cache = DemoCache(cache_file)
    cache.put("read_health", {"days": 14}, [])
    cache.save()
    d = deps()
    d.exists = lambda p: True
    out.clear()
    assert launcher.run_demo(cfg, deps=d, make_runtime=lambda c: SimpleNamespace(), serve_fn=lambda *a: None, out=out.append, offline_check=True) == 0
    assert "OFFLINE" in "\n".join(out) and "SIMULATED" in "\n".join(out)


def test_offline_mode_switches_the_model_off(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache = DemoCache(cache_file)
    cache.put("read_health", {"days": 14}, [])
    cache.save()
    cfg = Config(mode="demo-offline", state_dir=tmp_path / "s", now=NOW, cache_file=cache_file, llm_api_key="k", llm_model="m")
    d = deps()
    d.exists = lambda p: True
    seen = {}
    assert launcher.run_demo(cfg, deps=d, make_runtime=lambda c: seen.update(key=c.llm_api_key) or SimpleNamespace(), serve_fn=lambda *a: None, out=lambda s: None, offline_check=True) == 0
    assert seen["key"] is None


def test_the_real_runtime_starts_in_mock_mode(tmp_path, monkeypatch):
    """Not a fake: build the real Runtime and hand it to a fake server."""
    monkeypatch.delenv("CHIEF_WEB_TOKEN", raising=False)
    cfg = Config(mode="mock", state_dir=tmp_path / "s", now=NOW)
    got = {}
    code = launcher.run_demo(cfg, deps=deps(cloudflared=False), serve_fn=lambda rt, *a: got.update(rt=rt), out=lambda s: None, offline_check=True)
    assert code == 0 and got["rt"].cfg.mode == "mock" and "email_brief" in got["rt"].skills


# ---------------------------------------------------------------- QR


def test_qr_is_skipped_silently_without_a_package():
    def no_module(name):
        raise ImportError(name)

    lines: list[str] = []
    assert launcher.render_qr("https://x.example/?token=t", lines.append, import_module=no_module) is False
    assert lines == []


def test_qr_uses_qrcode_when_installed():
    class QR:
        def __init__(self, **kw):
            pass

        def add_data(self, data):
            self.data = data

        def print_ascii(self, out, invert):
            out.write("##  ##\n  ##  \n")

    lines: list[str] = []
    mod = SimpleNamespace(QRCode=QR)
    assert launcher.render_qr("u", lines.append, import_module=lambda n: mod) is True
    assert lines == ["##  ##\n  ##  "]


# ---------------------------------------------------------------- record-demo CLI


def test_record_demo_dry_run_calls_nothing_and_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "demo_cache.json"
    cfg = Config(mode="mock", state_dir=tmp_path / "s", cache_file=path)
    lines: list[str] = []
    assert launcher.record_demo_main(cfg, dry_run=True, out=lines.append) == 0
    text = "\n".join(lines)
    assert "Dry run" in text and all(p in text for p, _ in STORY) and not path.exists()


def test_record_demo_without_keys_explains(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    cfg = Config(mode="mock", state_dir=tmp_path / "s", cache_file=tmp_path / "c.json")
    lines: list[str] = []
    assert launcher.record_demo_main(cfg, out=lines.append) == 1
    assert "COMPOSIO_API_KEY, PARALLEL_API_KEY not set" in "\n".join(lines) and not (tmp_path / "c.json").exists()


def test_record_demo_writes_the_cache_and_keeps_a_backup(tmp_path):
    path = tmp_path / "demo_cache.json"
    cfg = Config(mode="mock", state_dir=tmp_path / "s", cache_file=path, now=NOW)
    lines: list[str] = []
    mock = MockTools(NOW)
    assert launcher.record_demo_main(cfg, tools=mock, out=lines.append) == 0
    assert len(DemoCache.load(path)) > 8 and mock.calls == []
    assert launcher.record_demo_main(cfg, tools=MockTools(NOW), out=lines.append) == 0
    assert (tmp_path / "demo_cache.json.bak").exists()
    assert "Saved" in "\n".join(lines)


def test_cli_wires_the_new_subcommands(tmp_path, monkeypatch, capsys):
    from chief.__main__ import main

    monkeypatch.setenv("CHIEF_MODE", "mock")
    monkeypatch.setenv("CHIEF_DEMO_CACHE", str(tmp_path / "c.json"))
    assert main(["record-demo", "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert main(["doctor", "--offline", "--port", "8765"]) in (0, 1)
    assert "Python version" in capsys.readouterr().out
