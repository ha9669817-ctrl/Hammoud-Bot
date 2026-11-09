"""اختبارات وحدات المحرك الجديد (engine/). لا تحتاج شبكة ولا Render."""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import backtest as bt  # noqa: E402
from engine import risk  # noqa: E402
from engine import signals as sg  # noqa: E402


def _flat_frame(n: int, price: float = 100.0) -> pd.DataFrame:
    t = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    c = np.full(n, price)
    return pd.DataFrame({"open_time": t, "open": c, "high": c + 1, "low": c - 1,
                         "close": c, "volume": 1.0})


def test_gold_blocked_below_500_only():
    assert risk.blocked_reason("XAUUSD", 20.0) is not None
    assert risk.blocked_reason("XAUUSD", 499.99) is not None
    assert risk.blocked_reason("XAUUSD", 500.0) is None
    assert risk.blocked_reason("BTCUSDT", 20.0) is None


def test_zero_balance_and_risk_amount_bounds():
    assert risk.is_zero_balance(0.0) and risk.is_zero_balance(0.5)
    assert not risk.is_zero_balance(20.0)
    assert risk.risk_amount(20.0) == 0.2
    assert risk.risk_amount(-5.0) == 0.0
    for bad in (0, -1, 6):
        try:
            risk.risk_amount(20.0, bad)
            raise AssertionError(f"risk_pct={bad} must be refused")
        except ValueError:
            pass


def test_signal_masks_have_right_length_and_never_overlap():
    rng = np.random.default_rng(7)
    n = 3000
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    t = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"open_time": t, "open": np.r_[close[0], close[:-1]],
                       "high": close + 0.8, "low": close - 0.8, "close": close, "volume": 1.0})
    for name, fn in sg.STRATEGIES.items():
        buy, sell = fn(df)
        assert len(buy) == n and len(sell) == n, name
        assert not np.any(buy & sell), name


def test_replay_stop_first_on_same_bar_conflict():
    df = _flat_frame(21)
    df.loc[20, "high"], df.loc[20, "low"] = 110.0, 90.0  # الوقف والهدف معاً
    buy = np.zeros(21, bool)
    buy[19] = True
    trades = bt.replay(df, buy, np.zeros(21, bool), cost_r=0.05)
    assert len(trades) == 1
    assert trades[0].r == -1.05, trades[0]


def test_replay_target_hit_pays_rr_minus_cost():
    df = _flat_frame(21)
    df.loc[20, "high"], df.loc[20, "low"] = 107.0, 99.0  # الهدف 106 فقط
    buy = np.zeros(21, bool)
    buy[19] = True
    trades = bt.replay(df, buy, np.zeros(21, bool), rr=2.0, cost_r=0.05)
    assert len(trades) == 1 and trades[0].r == 1.95, trades


def test_trades_do_not_overlap():
    df = _flat_frame(40)
    buy = np.zeros(40, bool)
    buy[19] = buy[20] = buy[21] = True  # إشارات متتالية داخل صفقة مفتوحة
    trades = bt.replay(df, buy, np.zeros(40, bool))
    assert len(trades) == 1


def test_metrics_known_values():
    ts = [bt.Trade("buy", 0, 1, 1, 0, 2, 1.95), bt.Trade("buy", 2, 3, 1, 0, 2, -1.05),
          bt.Trade("sell", 4, 5, 1, 2, 0, -1.05)]
    m = bt.metrics(ts)
    assert m["trades"] == 3
    assert m["win_rate"] == round(1 / 3, 4)
    assert m["profit_factor"] == round(1.95 / 2.1, 3)
    assert m["max_drawdown_r"] == 2.1
    assert bt.metrics([])["trades"] == 0


def test_end_to_end_backtest_runs_on_random_walk():
    rng = np.random.default_rng(3)
    n = 2000
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    t = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"open_time": t, "open": np.r_[close[0], close[:-1]],
                       "high": close + 0.8, "low": close - 0.8, "close": close, "volume": 1.0})
    buy, sell = sg.donchian_breakout(df)
    trades = bt.replay(df, buy, sell)
    m = bt.metrics(trades)
    assert m["trades"] == len(trades) and m["trades"] > 0
    assert set(m) >= {"win_rate", "profit_factor", "expectancy_r", "max_drawdown_r", "total_r"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
