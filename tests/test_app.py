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
        assert A.evolve_lock.locked() and not A.scan_busy()
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
    out = subprocess.run(["node", "--check", str(ROOT / "web" / "static" / "app.js")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[:300]


def test_page_and_assets_are_served():
    c = A.app.test_client()
    page = c.get("/").get_data(as_text=True)
    assert "/static/app.js" in page and 'id="statusBar"' in page and 'id="closedFilter"' in page
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/app.css").status_code == 200


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


def test_bitcoin_uses_lower_validation_threshold():
    def res(val_trades):
        m = lambda n: {"trades": n, "profit_factor": 1.5, "max_dd_r": 2.0, "expectancy_r": 0.2}
        return {"train": m(100), "val": m(val_trades)}
    assert A.passes(res(20), A.MIN_VAL_TRADES_BY_ASSET.get("BTCUSDT", A.MIN_VAL_TRADES)) is True
    assert A.passes(res(20), A.MIN_VAL_TRADES_BY_ASSET.get("XAUUSD", A.MIN_VAL_TRADES)) is False
    assert A.passes(res(30), A.MIN_VAL_TRADES_BY_ASSET.get("XAUUSD", A.MIN_VAL_TRADES)) is True


def test_gold_fallback_is_marked_as_proxy_and_closed():
    class Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return [[1700000000000 + i * 60000, "1", "1", "1", str(100 + i), "1"] for i in range(5)]
    original = (A.requests.get, A.in_session, A.ta_creds, A.MARKET_CACHE.copy())
    A.requests.get = lambda *a, **k: Resp()
    A.in_session = lambda asset, now=None: False if asset == "XAUUSD" else True
    A.ta_creds = lambda store: None
    try:
        out = A.live_market(force=True)
        assert out["XAUUSD"]["proxy"] is True and out["XAUUSD"]["session_open"] is False
        assert out["XAUUSD"]["source"] == "Binance"
    finally:
        A.requests.get, A.in_session, A.ta_creds = original[0], original[1], original[2]
        A.MARKET_CACHE.update(original[3])


def test_cycle_hard_deadline_releases_scan():
    gate = threading.Event()
    original_cycle, original_timeout = A.trading_cycle, A.CYCLE_TIMEOUT_S
    events = []
    original_log = A.log_event
    A.log_event = lambda kind, text, asset=None: events.append(text)
    A.trading_cycle = lambda asset, store, send_alerts=True: (gate.wait(10), _fake_cycle_result())[1]
    A.CYCLE_TIMEOUT_S = 0.5
    A.RUN.update(master=True, assets={"XAUUSD": True})
    try:
        start = time.time()
        A.run_trading(send_alerts=False)
        assert time.time() - start < 5, "scan must not wait for a hung asset"
        assert A.engine["status"] == "idle"
        assert any("تجاوز الفحص المهلة" in e for e in events), events
    finally:
        gate.set()
        A.trading_cycle, A.CYCLE_TIMEOUT_S = original_cycle, original_timeout
        A.log_event = original_log
        A.RUN.update(master=False, assets={})


def test_scheduler_tick_survives_a_crash():
    original = A.run_trading
    A.run_trading = lambda send_alerts=True: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        A._scheduler_tick()
        assert A.engine["status"] == "idle"
    finally:
        A.run_trading = original


def test_evolution_respects_time_budget():
    original = A.EVOLVE_BUDGET_S
    A.EVOLVE_BUDGET_S = -1
    try:
        out = A.run_evolution("BTCUSDT", None, None, A.store, 3, 8, seed=1)
        assert out["evaluated"] == 0
    finally:
        A.EVOLVE_BUDGET_S = original
        A.store.set_champion("BTCUSDT", None)


def test_watchdog_flags_a_stuck_scan():
    original = dict(A.engine)
    try:
        A.engine.update(status="analyzing", started_at=int(time.time()) - A.STUCK_S - 5)
        assert A._watchdog_check() is True
        A.engine.update(status="idle", started_at=None)
        assert A._watchdog_check() is False
    finally:
        A.engine.clear()
        A.engine.update(original)


def test_bbma_rule_is_registered_and_runs():
    import numpy as np
    import pandas as pd
    assert "bbma" in A.RULES and "bbma" in A.RULE_AR
    rng = np.random.default_rng(3)
    n = 4000
    close = 2000 + np.cumsum(rng.normal(0, 6, n))
    t = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"open_time": t, "open": close, "high": close + 4, "low": close - 4,
                       "close": close, "volume": 1.0})
    df["close_time"] = df["open_time"] + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1)
    sigs = A.rule_signals("bbma", df, 1.5, 2.0)
    assert isinstance(sigs, list) and all(x["risk"] > 0 for x in sigs)


def test_bb_reentry_rule_is_registered_and_runs():
    import numpy as np
    import pandas as pd
    assert "bb_reentry" in A.RULES and "bb_reentry" in A.RULE_AR
    rng = np.random.default_rng(11)
    n = 4000
    close = 2000 + np.cumsum(rng.normal(0, 6, n))
    t = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"open_time": t, "open": np.r_[close[0], close[:-1]], "high": close + 4,
                       "low": close - 4, "close": close, "volume": 1.0})
    df["close_time"] = df["open_time"] + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1)
    buy, sell = A._bb_reentry_masks(df)
    assert len(buy) == n and len(sell) == n
    sigs = A.rule_signals("bb_reentry", df, 1.5, 2.0)
    assert isinstance(sigs, list) and all(x["risk"] > 0 for x in sigs)


def test_trend_rules_registered_and_run():
    import numpy as np
    import pandas as pd
    new = ["donchian", "ema_pullback", "adx_di"]
    for r in new:
        assert r in A.RULES and r in A.RULE_AR
    rng = np.random.default_rng(21)
    n = 4000
    close = 2000 + np.cumsum(rng.normal(0, 6, n))
    t = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"open_time": t, "open": np.r_[close[0], close[:-1]], "high": close + 4,
                       "low": close - 4, "close": close, "volume": 1.0})
    df["close_time"] = df["open_time"] + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1)
    for r in new:
        up, dn = A._rule_masks(r, df)
        assert len(up) == n and len(dn) == n
        assert not np.any(np.asarray(up) & np.asarray(dn))
        sigs = A.rule_signals(r, df, 1.5, 2.0)
        assert isinstance(sigs, list) and all(x["risk"] > 0 for x in sigs)


def test_gold_blocked_below_threshold():
    assert A.asset_blocked_reason("XAUUSD", 20.0) is not None
    assert A.asset_blocked_reason("XAUUSD", 499.99) is not None
    assert A.asset_blocked_reason("XAUUSD", 500.0) is None
    assert A.asset_blocked_reason("BTCUSDT", 20.0) is None


def test_zero_balance_stops_master_and_alerts():
    import os
    saved_snap = A.store.meta_get("ta_snapshot")
    saved_master = A.RUN["master"]
    sent = []
    old_send = A.send
    try:
        A.send = lambda text: sent.append(text)
        A._alert_last.pop("zero_balance", None)
        A.RUN["master"] = True
        A.store.meta_set("ta_snapshot", {"balance": 0.0, "equity": 0.0})
        A.run_trading(send_alerts=False)
        assert A.RUN["master"] is False
        assert any("تصفير" in t for t in sent)
    finally:
        A.send = old_send
        A.RUN["master"] = saved_master
        A.store.meta_set("ta_snapshot", saved_snap)


def test_autostart_is_opt_in():
    import os
    saved_master = A.RUN["master"]
    saved_assets = dict(A.RUN["assets"])
    try:
        os.environ.pop("AUTOSTART", None)
        A.RUN["master"] = False
        assert A.apply_autostart() is False and A.RUN["master"] is False
        os.environ["AUTOSTART"] = "1"
        os.environ["AUTOSTART_ASSETS"] = "BTCUSDT"
        assert A.apply_autostart() is True
        assert A.RUN["master"] is True and A.RUN["assets"].get("BTCUSDT") is True
    finally:
        os.environ.pop("AUTOSTART", None)
        os.environ.pop("AUTOSTART_ASSETS", None)
        A.RUN["master"] = saved_master
        A.RUN["assets"] = saved_assets


def test_stuck_scan_does_not_block_next_scan():
    import time as _t
    saved = (A.SCAN_DEADLINE_S, A.trading_cycle, A.RUN["master"], dict(A.RUN["assets"]))
    snap_saved = A.store.meta_get("ta_snapshot")
    try:
        A.SCAN_DEADLINE_S = 1
        A.trading_cycle = lambda asset, store, send_alerts=True: _t.sleep(4) or {}
        A.RUN["master"] = True
        A.RUN["assets"]["BTCUSDT"] = True
        A.store.meta_set("ta_snapshot", {"balance": 500.0, "equity": 500.0})
        t0 = _t.time()
        A.run_trading(send_alerts=False)
        assert _t.time() - t0 < 3, "the scan must return at its deadline, not hang"
        assert A.scan_busy() is False, "an abandoned scan must not keep the engine busy"
        assert A.run_trading(send_alerts=False) is True, "the next scan must start"
    finally:
        A.SCAN_DEADLINE_S, A.trading_cycle = saved[0], saved[1]
        A.RUN["master"], A.RUN["assets"] = saved[2], saved[3]
        A.store.meta_set("ta_snapshot", snap_saved)


def test_binance_cooldown_after_418():
    saved = A._binance_block["until"]
    try:
        A._binance_block["until"] = A.time.time() + 100
        try:
            A.fetch_history("BTCUSDT", "1h", 10)
            raise AssertionError("expected cooldown error")
        except RuntimeError as exc:
            assert "محظور مؤقتاً" in str(exc)
    finally:
        A._binance_block["until"] = saved


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print(f"ALL {len(tests)} TESTS PASSED")
