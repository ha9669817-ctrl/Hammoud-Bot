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
    assert s["channels"] == ["Master blue", "Liquidity Trader"]


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
