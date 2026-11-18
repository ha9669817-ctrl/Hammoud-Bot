import importlib
import os
import sys

import pytest

STRUCT = "XAUUSD BUY\nEntry: 4038 - 4048\nSL: 4033\nTP1: 4053\nTP2: 4058\nTP3: 4063"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.delenv("SIGNALBOT_MODE", raising=False)
    for k in ("TICKERALL_API_KEY", "MT5_SERVER", "MT5_ACCOUNT_ID", "MT5_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    return app.app.test_client()


H = {"X-Admin-Password": "pw"}


def test_health_and_index(client):
    assert client.get("/health").json == {"ok": True}
    r = client.get("/")
    assert r.status_code == 200 and "بوت التوصيات" in r.get_data(as_text=True)


def test_state_default_off(client):
    s = client.get("/api/state").json
    assert s["on"] is False and s["mode_ready"] is False
    assert s["channels"] == ["Free Forex Signals", "Master blue", "Liquidity Trader", "Trade Sniper"]


def test_admin_routes_need_password(client):
    for method, path in [("post", "/api/signalbot/toggle"), ("post", "/api/signalbot/message"),
                         ("post", "/api/history/start"), ("get", "/api/history/status"),
                         ("get", "/api/history/zip")]:
        assert getattr(client, method)(path, json={}).status_code == 401


def test_toggle_on_refused_without_demo_mode(client):
    r = client.post("/api/signalbot/toggle", json={"on": True}, headers=H)
    assert r.status_code == 400 and client.get("/api/state").json["on"] is False


def test_manual_message_dry_parse_and_validation(client):
    r = client.post("/api/signalbot/message", json={"channel": "Master blue", "text": STRUCT, "dry": True}, headers=H)
    assert r.json["parsed"]["side"] == "buy"
    assert client.post("/api/signalbot/message", json={"channel": "x", "text": STRUCT}, headers=H).status_code == 400
    r = client.post("/api/signalbot/message", json={"channel": "Master blue", "text": "hello"}, headers=H)
    assert r.json["ok"] is False
    # تنفيذ حقيقي بلا بوت مشغّل يُرفض بوضوح
    r = client.post("/api/signalbot/message", json={"channel": "Master blue", "text": STRUCT}, headers=H)
    assert r.status_code == 400


def test_history_start_requires_broker(client):
    r = client.post("/api/history/start", json={}, headers=H)
    assert r.status_code == 400


def test_keepalive_noop_without_render_url(client, monkeypatch):
    import app as appmod
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert appmod.keepalive_once() is False


def test_boot_does_not_wait_for_hanging_persistence(client, monkeypatch):
    import time as _t
    import app as appmod

    class FakePersist:
        def __init__(self):
            self.state = {"enabled": True, "error": None}
        def restore(self):
            _t.sleep(30)                         # يعلّق
        def start(self):
            pass

    class FakeEx:
        def __init__(self, store): pass

    class FakeBot:
        def __init__(self, *a, **k):
            self.j = object()
            self.trades = {}

    class FakeRunner:
        def __init__(self, *a, **k): self.state = {}
        def start(self): pass
        def push(self, *a): pass

    class FakeTg:
        def __init__(self, *a, **k): self.state = {}
        def start(self): pass

    monkeypatch.setattr(appmod, "RESTORE_WAIT_S", 0.2)
    monkeypatch.setattr(appmod.Persist, "from_env", classmethod(lambda cls, f, s: FakePersist()))
    monkeypatch.setattr(appmod, "SignalExecutor", FakeEx)
    monkeypatch.setattr(appmod, "SignalBot", FakeBot)
    monkeypatch.setattr(appmod, "Runner", FakeRunner)
    monkeypatch.setattr(appmod, "ManualManager", lambda *a, **k: None)
    monkeypatch.setattr(appmod, "TelegramIngest", FakeTg)
    monkeypatch.setattr(appmod, "Journal", lambda p: object())
    appmod.SB.update(bot=None, persist=None, error=None)
    t0 = _t.time()
    appmod.boot()
    assert _t.time() - t0 < 5
    assert appmod.SB["bot"] is not None and appmod.SB["stage"] == "تعمل"
    assert "لم تنتهِ" in appmod.SB["persist"].state["error"]
    appmod.SB.update(bot=None, persist=None)


def test_stuck_where_reports_blocked_thread(client):
    import threading
    import time as _t
    import app as appmod
    ev = threading.Event()

    def blocked():
        appmod.SB["thread"] = threading.get_ident()
        ev.wait(5)
    appmod.SB.update(bot=None)
    th = threading.Thread(target=blocked, daemon=True)
    th.start()
    _t.sleep(0.2)
    where = appmod._stuck_where()
    ev.set()
    assert where and any("blocked" in ln or "wait" in ln for ln in where)
