"""إشارات على الشموع المغلقة. كل دالة تعيد (شراء، بيع) كمصفوفتين منطقيتين بطول الشموع.
الإشارة تُحسب عند إغلاق الشمعة، والدخول يكون بعدها."""
import numpy as np
import pandas as pd


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _fresh_cross_up(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a > b) & (a.shift(1) <= b.shift(1))).fillna(False).to_numpy()


def _fresh_cross_dn(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a < b) & (a.shift(1) >= b.shift(1))).fillna(False).to_numpy()


def donchian_breakout(df: pd.DataFrame, n: int = 20):
    """إغلاق يكسر أعلى قمة لآخر n شمعة (أو أدنى قاع) لأول مرة."""
    close = df["close"]
    up_lvl = df["high"].rolling(n).max().shift(1)
    dn_lvl = df["low"].rolling(n).min().shift(1)
    return _fresh_cross_up(close, up_lvl), _fresh_cross_dn(close, dn_lvl)


def ema_pullback(df: pd.DataFrame, fast: int = 21, slow: int = 200):
    """ارتداد مع الاتجاه: عبور EMA السريع في اتجاه EMA البطيء."""
    close = df["close"]
    e_fast, e_slow = _ema(close, fast), _ema(close, slow)
    buy = _fresh_cross_up(close, e_fast) & (close > e_slow).to_numpy()
    sell = _fresh_cross_dn(close, e_fast) & (close < e_slow).to_numpy()
    return buy, sell


def adx_di(df: pd.DataFrame, n: int = 14, min_adx: float = 20.0):
    """تقاطع +DI و-DI بمعادلات Wilder، بشرط أن يكون ADX يدل على اتجاه."""
    h, l, c = df["high"], df["low"], df["close"]
    up_move, dn_move = h.diff(), -l.diff()
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_w
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_w
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    strong = (adx > min_adx).fillna(False).to_numpy()
    return _fresh_cross_up(pdi, mdi) & strong, _fresh_cross_dn(pdi, mdi) & strong


STRATEGIES = {"donchian": donchian_breakout, "ema_pullback": ema_pullback, "adx_di": adx_di}
