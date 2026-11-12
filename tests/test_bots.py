"""اختبارات كتالوج البوتات: كل بوت يعمل، ويعيد مصفوفتين بالطول الصحيح، ولا يشتري ويبيع في الشمعة نفسها."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bots  # noqa: E402


def _frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 5, n))
    t = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open_time": t, "open": open_,
                         "high": np.maximum(open_, close) + rng.uniform(0.5, 3, n),
                         "low": np.minimum(open_, close) - rng.uniform(0.5, 3, n),
                         "close": close, "volume": rng.uniform(50, 500, n)})


def test_every_bot_runs_and_returns_aligned_masks():
    df = _frame(3000, 42)
    for name, fn in bots.BOT_FUNCS.items():
        buy, sell = fn(df)
        assert len(buy) == len(df) and len(sell) == len(df), name
        assert buy.dtype == bool and sell.dtype == bool, name
        assert not np.any(buy & sell), name


def test_catalogue_has_adaptive_bot_and_gold_reversion_bot():
    assert set(bots.BOT_FUNCS) == {"ai_brain", "bb_revert"} == set(bots.BOT_AR)


def test_every_bot_has_an_arabic_name():
    assert set(bots.BOT_FUNCS) == set(bots.BOT_AR)


def test_bots_survive_flat_and_short_data():
    flat = _frame(60, 1)
    flat[["open", "high", "low", "close"]] = 100.0
    flat["volume"] = 1.0
    for name, fn in bots.BOT_FUNCS.items():
        buy, sell = fn(flat)
        assert len(buy) == 60 and len(sell) == 60, name


def test_no_grid_bot_is_claimed():
    assert "grid" not in bots.BOT_FUNCS


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
