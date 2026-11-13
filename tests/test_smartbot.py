import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartbot.engine import SmartBot  # noqa: E402
from smartbot.features import ATOM_NAMES, atom_masks, build  # noqa: E402
from smartbot.journal import Journal  # noqa: E402
from smartbot.strategy import COST_R, Spec, signal_mask, simulate  # noqa: E402


def synthetic(n=1500, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 1, n)
    l = np.minimum(o, c) - rng.uniform(0, 1, n)
    t = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"time": t.astype(str), "open": o, "high": h, "low": l, "close": c})


class TestCausality(unittest.TestCase):
    def test_features_at_t_do_not_depend_on_future_bars(self):
        df = synthetic()
        t = 1200
        full = build(df)
        part = build(df.iloc[: t + 1].reset_index(drop=True))
        for k in full:
            a, b = full[k][t], part[k][t]
            self.assertTrue((np.isnan(a) and np.isnan(b)) or np.isclose(a, b, rtol=1e-9, atol=1e-9),
                            f"feature {k} uses future data")
        fm, pm = atom_masks(full), atom_masks(part)
        for name in ATOM_NAMES:
            for side in (0, 1):
                self.assertEqual(bool(fm[name][side][t]), bool(pm[name][side][t]), f"atom {name} leaks")


class TestSimulate(unittest.TestCase):
    def test_target_hit_gives_rr_minus_cost(self):
        f = {
            "open": np.array([100.0, 100.0, 100.0]),
            "high": np.array([100.5, 100.5, 105.0]),
            "low": np.array([99.5, 99.5, 99.5]),
            "close": np.array([100.0, 100.0, 104.0]),
            "atr14": np.array([1.0, 1.0, 1.0]),
        }
        spec = Spec(side=1, atoms=("trend_ema20_50",), stop_atr=1.0, rr=2.0, max_bars=5)
        sig = np.array([True, False, False])
        R = simulate(f, sig, spec)
        self.assertEqual(len(R), 1)
        self.assertAlmostEqual(R[0], 2.0 - COST_R)


class TestEngineMatchesSimulate(unittest.TestCase):
    def test_engine_trades_equal_offline_simulation(self):
        df = synthetic(1500)
        f = build(df)
        spec = Spec(side=1, atoms=("above_ema200", "rsi_mid"), stop_atr=1.5, rr=2.0, max_bars=24)
        offline = simulate(f, signal_mask(atom_masks(f), spec), spec)
        with tempfile.TemporaryDirectory() as d:
            bot = SmartBot(Journal(d), start_balance=1000.0)
            bot.spec = spec
            bot.learn = False  # نقارن التنفيذ وحده، بلا إيقاف أو تجنب
            for r in df.itertuples(index=False):
                bot.feed(r.time, r.open, r.high, r.low, r.close)
            live = [t["r_net"] for t in bot.j.read_jsonl(bot.j.trades_path, limit=10000)]
        self.assertGreater(len(offline), 0, "synthetic data produced no trades")
        n = min(len(live), len(offline))
        self.assertGreaterEqual(n, len(offline) - 1)  # آخر صفقة قد تبقى مفتوحة وقت انتهاء البيانات
        # الدفتر يحفظ النتيجة مقرّبة إلى 4 منازل، لذلك التسامح 1e-4
        np.testing.assert_allclose(live[:n], offline[:n], atol=1e-4)


class TestLearning(unittest.TestCase):
    def _bot(self, d):
        bot = SmartBot(Journal(d), start_balance=1000.0)
        bot.hist = [(str(i), 100.0, 101.0, 99.0, 100.0) for i in range(10)]
        return bot

    def test_strategy_retired_after_losing_streak(self):
        with tempfile.TemporaryDirectory() as d:
            bot = self._bot(d)
            spec = Spec(side=1, atoms=("breakout20",), stop_atr=1.0, rr=2.0)
            bot.spec = spec
            for i in range(10):
                bot.j.trade({"spec_key": spec.key, "r_net": -1.0, "side": "شراء", "bucket": "50-70"})
            events = []
            bot._learn({"r_net": -1.0}, events)
            self.assertIn(spec.key, bot.retired)
            self.assertIsNone(bot.spec)
            self.assertTrue(any(e.get("kind") == "lesson" for e in events))

    def test_bad_rsi_bucket_is_avoided(self):
        with tempfile.TemporaryDirectory() as d:
            bot = self._bot(d)
            bot.spec = Spec(side=1, atoms=("breakout20",), stop_atr=1.0, rr=2.0)
            for i in range(8):
                bot.j.trade({"spec_key": "other", "r_net": -1.0 if i else 1.0, "side": "شراء", "bucket": "50-70"})
            bot._learn({"r_net": -1.0}, [])
            self.assertIn("شراء|50-70", bot.avoid)


class FakeClient:
    def __init__(self, positions=None, profit=None):
        self._pos = positions or []
        self._profit = profit
        self.orders = []

    def positions(self):
        return list(self._pos)

    def spec(self, symbol):
        return {"name": symbol, "contractSize": 100, "volumeStep": 0.01, "volumeMin": 0.01}

    def open_market(self, symbol, side, volume, sl, tp):
        self.orders.append((symbol, side, volume, sl, tp))
        return {"ticket": 555}

    def closed_profit(self, ticket, since_iso):
        return self._profit


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("SMARTBOT_MODE")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SMARTBOT_MODE", None)
        else:
            os.environ["SMARTBOT_MODE"] = self._old

    def test_refuses_without_explicit_demo_mode(self):
        from smartbot.executor import LiveGuardError, TickerAllExecutor
        os.environ.pop("SMARTBOT_MODE", None)
        with self.assertRaises(LiveGuardError):
            TickerAllExecutor(FakeClient(), lambda *a: 0.01)
        os.environ["SMARTBOT_MODE"] = "live"
        with self.assertRaises(LiveGuardError):
            TickerAllExecutor(FakeClient(), lambda *a: 0.01)

    def test_open_sends_stop_and_target_and_returns_ticket(self):
        from smartbot.executor import TickerAllExecutor
        c = FakeClient()
        ex = TickerAllExecutor(c, lambda spec, risk, dist: 0.02, mode="demo")
        r = ex.open("XAUUSDm", "buy", 4000.0, 3990.0, 4020.0, 10.0, "smartbot")
        self.assertTrue(r["opened"]); self.assertEqual(r["ticket"], 555)
        self.assertEqual(c.orders, [("XAUUSDm", "buy", 0.02, 3990.0, 4020.0)])

    def test_no_second_position_on_same_symbol(self):
        from smartbot.executor import TickerAllExecutor
        c = FakeClient(positions=[{"symbol": "XAUUSDm", "ticket": 9}])
        ex = TickerAllExecutor(c, lambda *a: 0.01, mode="demo")
        r = ex.open("XAUUSDm", "sell", 4000.0, 4010.0, 3980.0, 10.0, "x")
        self.assertFalse(r["opened"]); self.assertEqual(c.orders, [])

    def test_closed_detected_and_profit_from_history(self):
        from smartbot.executor import TickerAllExecutor
        ex = TickerAllExecutor(FakeClient(positions=[], profit=12.5), lambda *a: 0.01, mode="demo")
        self.assertEqual(ex.check_closed(555, "XAUUSDm", "2026-11-13T00:00:00Z"), (True, 12.5))

    def test_manual_close_is_refused_until_verified(self):
        from smartbot.executor import TickerAllExecutor
        ex = TickerAllExecutor(FakeClient(), lambda *a: 0.01, mode="demo")
        with self.assertRaises(NotImplementedError):
            ex.close_now(555)


class FakeLiveExecutor:
    """منفذ وهمي: يفتح بتذكرة، ويعلن الإغلاق بربح ثابت عند أول استعلام بعد الفتح."""
    def __init__(self, profit=10.0, accept=True):
        self.opened = []
        self.profit = profit
        self.accept = accept
        self._closed_next = {}

    def open(self, symbol, side, entry_ref, stop, target, risk_money, comment):
        if not self.accept:
            return {"opened": False, "reason": "مرفوض للتجربة"}
        self.opened.append((symbol, side, entry_ref, stop, target, risk_money))
        ticket = 1000 + len(self.opened)
        self._closed_next[ticket] = 0
        return {"opened": True, "ticket": ticket, "volume": 0.01, "comment": comment}

    def check_closed(self, ticket, symbol, since_iso):
        self._closed_next[ticket] = self._closed_next.get(ticket, 0) + 1
        if self._closed_next[ticket] >= 1:
            return True, self.profit
        return False, None


class TestLiveRunner(unittest.TestCase):
    def _df(self):
        return synthetic(1500)

    def test_live_runner_sends_stop_target_and_records_broker_close(self):
        from smartbot.runner import LiveRunner
        df = self._df()
        with tempfile.TemporaryDirectory() as d:
            bot = SmartBot(Journal(d), start_balance=1000.0)
            bot.spec = Spec(side=1, atoms=("above_ema200", "rsi_mid"), stop_atr=1.5, rr=2.0, max_bars=24)
            ex = FakeLiveExecutor(profit=10.0)
            runner = LiveRunner(bot, ex, "XAUUSDm", lambda: df)
            runner.tick()
            self.assertGreater(len(ex.opened), 0, "no order was sent")
            symbol, side, entry, stop, target, risk = ex.opened[0]
            self.assertEqual(symbol, "XAUUSDm")
            self.assertEqual(side, "buy")
            self.assertLess(stop, entry)
            self.assertGreater(target, entry)
            closes = bot.j.read_jsonl(bot.j.trades_path, limit=1000)
            self.assertTrue(any(t["reason"] == "broker" for t in closes))
            self.assertAlmostEqual(bot.balance, 1000.0 + 10.0 * len([t for t in closes if t["reason"] == "broker"]), places=6)

    def test_refused_order_leaves_no_position(self):
        from smartbot.runner import LiveRunner
        df = self._df()
        with tempfile.TemporaryDirectory() as d:
            bot = SmartBot(Journal(d), start_balance=1000.0)
            bot.spec = Spec(side=1, atoms=("above_ema200", "rsi_mid"), stop_atr=1.5, rr=2.0, max_bars=24)
            runner = LiveRunner(bot, FakeLiveExecutor(accept=False), "XAUUSDm", lambda: df)
            runner.tick()
            self.assertIsNone(bot.pos)
            self.assertEqual(bot.balance, 1000.0)


if __name__ == "__main__":
    unittest.main()
