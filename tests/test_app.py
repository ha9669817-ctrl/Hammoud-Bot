"""اختبارات مستقلة للتطبيق. تعمل بـ pytest أو بتشغيل الملف مباشرة."""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ma-tests-")
os.environ.update({"DISABLE_SCHEDULER": "1", "DB_PATH": str(Path(TMP) / "bot.db"),
                   "ADMIN_PASSWORD": "test-pass-123"})
sys.path.insert(0, str(ROOT))
import app as A  # noqa: E402

A.log_event = lambda *a, **k: None
PW = {"X-Admin-Password": "test-pass-123"}
CHAMP = {"genome": {"rule": "rsi", "tf": 2}, "score": 1.0, "result": {"pf": 2.0}, "since": "x"}


def _candle(i, step):
    t = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=step * i)
    return {"time": t.isoformat(), "open": 100 + i, "high": 101 + i, "low": 99 + i,
            "close": 100.5 + i, "tick_volume": 5}


class FakeCandles:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _acct(self, method, path, params=None):
        self.calls.append(params)
        name = "range" if "from" in params else ("hours" if "hours" in params else "count")
        return self.answers.get(name, {"candles": [], "served": None, "count": 0})


def test_routes_and_auth():
    c = A.app.test_client()
    assert c.get("/health").status_code == 200
    assert c.get("/api/state").status_code == 200
    assert c.get("/").status_code == 200
    assert c.post("/api/evolve").status_code == 401
    assert c.get("/api/champions/export").status_code == 401
    assert c.get("/api/trades.csv").status_code == 401


def test_candle_request_forms_fall_through():
    ok = lambda n, step=3600: {"candles": [_candle(i, step) for i in range(n)]}
    c = FakeCandles({"range": ok(400)})
    assert len(A.ta_history("XAUUSD", "1h", 3000, client=c)) == 400 and len(c.calls) == 1
    c = FakeCandles({"count": ok(300)})
    assert len(A.ta_history("XAUUSD", "1h", 3000, client=c)) == 300 and len(c.calls) == 2
    c = FakeCandles({"hours": ok(50)})
    assert len(A.ta_history("XAUUSD", "1h", 3000, client=c)) == 50 and c.calls[2]["hours"] == 1440
    c = FakeCandles({})
    try:
        A.ta_history("XAUUSD", "1h", 3000, client=c)
        raise AssertionError("expected NO_CANDLES")
    except A.TickerAllError as exc:
        assert len(c.calls) == 3 and "السوق مغلق" in str(exc)


def test_champions_persist_and_restore():
    A.store.set_champion("XAUUSD", CHAMP)
    assert json.loads((A.CHAMPION_DIR / "XAUUSD.json").read_text(encoding="utf-8")) == CHAMP
    fresh = A.Store(str(Path(TMP) / "fresh.db"), 1000.0)
    assert A.restore_champions(fresh) == ["XAUUSD"] and fresh.get_champion("XAUUSD") == CHAMP
    assert A.restore_champions(fresh) == []
    A.store.set_champion("XAUUSD", None)
    assert not (A.CHAMPION_DIR / "XAUUSD.json").exists()


def test_seed_file_restores_champions():
    seed = Path(TMP) / "seed.json"
    seed.write_text(json.dumps({"BTCUSDT": CHAMP}), encoding="utf-8")
    original = A.SEED_CHAMPIONS_FILE
    A.SEED_CHAMPIONS_FILE = seed
    try:
        s = A.Store(str(Path(TMP) / "seeded.db"), 1000.0)
        assert A.restore_champions(s) == ["BTCUSDT"] and s.get_champion("BTCUSDT") == CHAMP
    finally:
        A.SEED_CHAMPIONS_FILE = original


def test_next_open_for_gold_only():
    sat = datetime(2026, 11, 7, 16, 0, tzinfo=timezone.utc)
    assert A.next_open_utc("XAUUSD", sat) == "2026-11-09T00:00:00+00:00"
    assert A.next_open_utc("XAUUSD", datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)) is None
    assert A.next_open_utc("BTCUSDT", sat) is None


def test_evolution_does_not_block_trading():
    gate = threading.Event()
    original = A.evolve_asset
    A.evolve_asset = lambda *a, **k: (gate.wait(5), {"changed": False})[1]
    A.store.get_assets = lambda: ["XAUUSD"]
    try:
        t = threading.Thread(target=A.run_evolution_job, daemon=True)
        t.start()
        time.sleep(0.2)
        assert A.evolve_lock.locked() and not A.work_lock.locked()
        assert A.run_evolution_job() is False
        gate.set()
        t.join(5)
    finally:
        A.evolve_asset = original


def test_reconcile_flags_missing_positions():
    class Broker:
        def positions(self):
            return [{"symbol": "XAUUSD", "ticket": "555"}]
    original = (A.ta_creds, A.ta_client)
    A.ta_creds = lambda store: {"login": "1"}
    A.ta_client = lambda store: Broker()
    A.store.set_mode("tickerall")
    try:
        A.store.open_trade("XAUUSD", "k", "buy", int(time.time() * 1000), 2000.0, 1990.0,
                           2020.0, 10.0, 10.0, mode="tickerall", qty=0.01, order_id="999")
        result = A.reconcile_with_broker()
        assert result["missing_at_broker"] == 1, result
    finally:
        A.ta_creds, A.ta_client = original
        A.store.set_mode("paper")


def test_reconcile_skips_when_ticket_field_unknown():
    class Broker:
        def positions(self):
            return [{"symbol": "XAUUSD", "unknownKey": 1}]
    original = (A.ta_creds, A.ta_client)
    A.ta_creds = lambda store: {"login": "1"}
    A.ta_client = lambda store: Broker()
    A.store.set_mode("tickerall")
    try:
        assert A.reconcile_with_broker() is None
    finally:
        A.ta_creds, A.ta_client = original
        A.store.set_mode("paper")


def test_alerts_are_throttled():
    sent = []
    original = A.send
    A.send = sent.append
    try:
        A.alert_once("k-test", "one", every=1800)
        A.alert_once("k-test", "two", every=1800)
        assert sent == ["one"]
    finally:
        A.send = original


def test_info_logged_only_on_change():
    seen = []
    original = A.log_event
    A.log_event = lambda kind, text, asset=None: seen.append(text)
    try:
        A.log_info_once("ZZZ", "same")
        A.log_info_once("ZZZ", "same")
        A.log_info_once("ZZZ", "changed")
        assert seen == ["same", "changed"]
    finally:
        A.log_event = original


def test_trades_csv_export():
    c = A.app.test_client()
    r = c.get("/api/trades.csv", headers=PW)
    assert r.status_code == 200 and r.mimetype == "text/csv"
    header = next(csv.reader(io.StringIO(r.get_data(as_text=True))))
    assert header[:3] == ["id", "asset", "side"]


def test_state_reports_memory_and_next_open():
    st = A.app.test_client().get("/api/state").get_json()
    assert "peak_mb" in st["engine"]
    assert "next_open" in st["assets"]["XAUUSD"] if st["assets"] else True


def test_browser_script_parses():
    if not shutil.which("node"):
        return
    scripts = re.findall(r"<script>(.*?)</script>", A.INDEX_HTML, re.S)
    path = Path(TMP) / "page.js"
    path.write_text("\n".join(scripts), encoding="utf-8")
    out = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[:300]


def _fake_cycle_result():
    return {"price": 1.0, "latest": {}, "has_champion": True, "errors": [], "note": None,
            "ta_positions": [], "ta_account": None, "opened": None, "closed": []}


def test_trading_is_off_by_default_and_respects_switches():
    A.RUN.update(master=False, assets={})
    calls = []
    original = A.trading_cycle
    A.trading_cycle = lambda asset, store, send_alerts=True: (calls.append(asset), _fake_cycle_result())[1]
    try:
        A.run_trading(send_alerts=False)
        assert calls == [], "nothing may trade while the master switch is off"
        A.RUN["master"] = True
        A.RUN["assets"] = {"XAUUSD": True}
        A.run_trading(send_alerts=False)
        assert calls == ["XAUUSD"], calls
    finally:
        A.trading_cycle = original
        A.RUN.update(master=False, assets={})


def test_control_endpoints_require_password():
    c = A.app.test_client()
    assert c.post("/api/trading/master", json={"on": True}).status_code == 401
    assert c.post("/api/trading/asset", json={"asset": "XAUUSD", "on": True}).status_code == 401
    assert c.post("/api/strategy/select", json={"asset": "XAUUSD", "index": 0}).status_code == 401


def test_manual_strategy_choice_is_locked_against_evolution():
    A.store.save_strategy("BTCUSDT", {"rule": "rsi", "tf": 2}, "accepted", 1.5,
                          {"val": {"win_rate": 50, "profit_factor": 1.8, "max_dd_r": 3}})
    c = A.app.test_client()
    r = c.post("/api/strategy/select", json={"asset": "BTCUSDT", "index": 0}, headers=PW)
    assert r.status_code == 200, r.get_json()
    assert A.store.get_champion("BTCUSDT")["genome"] == {"rule": "rsi", "tf": 2}
    state = c.get("/api/state").get_json()
    assert A.store.meta_get("lock|BTCUSDT") is True
    assert isinstance(state["trading"]["locked"], dict)
    original = A.load_market
    A.load_market = lambda asset: (_ for _ in ()).throw(AssertionError("evolution must not run on a locked asset"))
    try:
        res = A.evolve_asset("BTCUSDT", A.store, 1, 4, False)
        assert res["locked"] is True and res["changed"] is False
    finally:
        A.load_market = original
    assert c.post("/api/strategy/unlock", json={"asset": "BTCUSDT"}, headers=PW).status_code == 200
    assert A.store.meta_get("lock|BTCUSDT") in (None, False)


def test_bad_strategy_index_is_refused():
    c = A.app.test_client()
    r = c.post("/api/strategy/select", json={"asset": "XAUUSD", "index": 999}, headers=PW)
    assert r.status_code == 400


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print(f"ALL {len(tests)} TESTS PASSED")
