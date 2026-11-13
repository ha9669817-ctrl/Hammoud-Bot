"""مؤشرات سببية: كل قيمة عند الشمعة i لا تعتمد إلا على الشموع حتى i."""
import numpy as np
import pandas as pd


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(n).mean()


def build(df: pd.DataFrame) -> dict:
    """يحوّل الشموع (open, high, low, close مرتبة زمنياً) إلى مصفوفات مؤشرات."""
    c, o, h, l = df["close"], df["open"], df["high"], df["low"]
    a = _atr(df, 14)
    m = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    rng = (h - l).replace(0, np.nan)
    f = {
        "open": o, "high": h, "low": l, "close": c,
        "ema20": _ema(c, 20), "ema50": _ema(c, 50), "ema200": _ema(c, 200),
        "rsi14": _rsi(c, 14), "atr14": a,
        "bb_mid": m, "bb_up": m + 2 * sd, "bb_lo": m - 2 * sd,
        # أعلى/أدنى 20 شمعة السابقة (بدون الشمعة الحالية)
        "dc_hi20": h.rolling(20).max().shift(1),
        "dc_lo20": l.rolling(20).min().shift(1),
        "body_ratio": (c - o) / rng,  # موجب = شمعة صاعدة
    }
    return {k: v.to_numpy(dtype=float) for k, v in f.items()}


def atom_masks(f: dict) -> dict:
    """شروط بسيطة قابلة للتركيب. لكل شرط: (قيمة للشراء، قيمة للبيع)."""
    c, o, h, l = f["close"], f["open"], f["high"], f["low"]
    e20, e50, e200 = f["ema20"], f["ema50"], f["ema200"]
    r, a = f["rsi14"], f["atr14"]
    bm, bu, bl = f["bb_mid"], f["bb_up"], f["bb_lo"]
    dh, dl = f["dc_hi20"], f["dc_lo20"]
    br = f["body_ratio"]
    bull = (br > 0.5)
    bear = (br < -0.5)
    return {
        "trend_ema20_50": (e20 > e50, e20 < e50),
        "above_ema200": (c > e200, c < e200),
        "rsi_extreme": (r < 35, r > 65),
        "rsi_mid": (r < 45, r > 55),
        "bb_band_touch": (c < bl, c > bu),
        "bb_side": (c < bm, c > bm),
        "breakout20": (c > dh, c < dl),
        "strong_candle": (bull, bear),
        "pullback_ema50": (l <= e50 + 0.5 * a, h >= e50 - 0.5 * a),
    }


ATOM_NAMES = [
    "trend_ema20_50", "above_ema200", "rsi_extreme", "rsi_mid", "bb_band_touch",
    "bb_side", "breakout20", "strong_candle", "pullback_ema50",
]
