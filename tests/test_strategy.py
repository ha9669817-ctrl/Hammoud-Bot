import tempfile

import numpy as np
import pandas as pd

from signalbot.engine import Journal
from strategy import indicators as ind, signal as sg
from strategy.live import Analyst, MIN_BARS


def _market(drift, n=8000, seed=3):
    rng = np.random.default_rng(seed)
    close = 3000 + np.cumsum(rng.normal(drift, 0.6, n))
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) + rng.uniform(0.1, 0.8, n)
    lo = np.minimum(op, close) - rng.uniform(0.1, 0.8, n)
    idx = pd.date_range("2026-01-05", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close}, index=idx)


def _first_signal(m5):
    d = sg.prepare(m5)
    for i in range(3000, len(d) - 2):
        dec = sg.decide(d, i, **sg.LIVE)
        if dec:
            return i, dec
    return None, None


def test_uptrend_only_buys_and_downtrend_only_sells():
    for drift, want in ((0.08, "buy"), (-0.08, "sell")):
        m5 = _market(drift)
        d = sg.prepare(m5)
        sides = {sg.decide(d, i, **sg.LIVE)[0] for i in range(3000, len(d) - 2) if sg.decide(d, i, **sg.LIVE)}
        assert sides == {want}, (drift, sides)      # لا دخول ضد الاتجاه أبداً


def test_no_lookahead_decision_unchanged_by_future_bars():
    m5 = _market(0.08)
    i, dec = _first_signal(m5)
    assert dec
    cut = sg.prepare(m5.iloc[: i + 1])
    assert sg.decide(cut, len(cut) - 1, **sg.LIVE) == dec


class Ex:
    def __init__(self, m5):
        self.m5, self.placed, self.live = m5, [], {}

    def candle_rows(self, tf, n, symbol=None):
        rule = {"M5": None, "M15": "15min", "H1": "1h", "H4": "4h"}[tf]
        df = self.m5 if rule is None else ind.resample(self.m5, rule)
        return [(t.timestamp(), r.open, r.high, r.low, r.close) for t, r in df.tail(n).iterrows()]

    def open_tickets_for(self, symbol): return dict(self.live)
    def place_for(self, symbol, side, sl, tp=None):
        self.placed.append((side, sl, tp, symbol)); self.live[7] = "open"; return 7


def test_analyst_enters_once_with_trend_and_no_tp():
    full = _market(0.08)
    i, dec = _first_signal(full)
    m5 = full.iloc[: i + 2]                       # الشمعة i مكتملة وبعدها جارية
    ex = Ex(m5)
    a = Analyst(ex, Journal(tempfile.mkdtemp()), lambda: True, lambda: True, now=lambda: 1000.0, symbol="EURUSDm")
    a.tick()
    assert len(ex.placed) == 1 and ex.placed[0][0] == "buy" and ex.placed[0][2] is None and ex.placed[0][1] < m5["close"].iloc[-1]
    a._last_run = 0; a.now = lambda: 1100.0
    a.tick()
    assert len(ex.placed) == 1                    # صفقة مفتوحة: لا دخول ثانٍ


def test_analyst_off_or_main_off_does_nothing_and_errors_are_isolated():
    full = _market(0.08)
    i, _ = _first_signal(full)
    ex = Ex(full.iloc[: i + 2])
    for en, own in ((False, True), (True, False)):
        a = Analyst(ex, Journal(tempfile.mkdtemp()), lambda en=en: en, lambda own=own: own, now=lambda: 500.0, symbol="EURUSDm")
        a.tick()
        assert not ex.placed
    ex.candle_rows = lambda tf, n, symbol=None: []             # الوسيط لا يعيد شموعاً
    a = Analyst(ex, Journal(tempfile.mkdtemp()), lambda: True, lambda: True, now=lambda: 900.0, symbol="EURUSDm")
    a.tick()
    assert a.state["error"] and not ex.placed


def test_analyst_refuses_gold_and_does_nothing_without_symbol():
    full = _market(0.08)
    i, _ = _first_signal(full)
    ex = Ex(full.iloc[: i + 2])
    for sym in ("XAUUSDm", "GOLD", ""):
        a = Analyst(ex, Journal(tempfile.mkdtemp()), lambda: True, lambda: True, now=lambda: 500.0, symbol=sym)
        a.tick()
        assert not ex.placed
