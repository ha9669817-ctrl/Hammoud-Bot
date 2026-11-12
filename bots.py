"""كتالوج البوتات: منطق كل بوت مكتوب هنا بالكامل.

كل بوت دالة نقية: تأخذ شموع الأصل وتعيد (شراء، بيع) كمصفوفتين منطقيتين.
الإشارة تُحسب عند إغلاق الشمعة، والدخول بعدها.
"""
import numpy as np
import pandas as pd


def _up(a: pd.Series, b) -> np.ndarray:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return ((a > b) & (a.shift(1) <= b.shift(1))).fillna(False).to_numpy()


def _dn(a: pd.Series, b) -> np.ndarray:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return ((a < b) & (a.shift(1) >= b.shift(1))).fillna(False).to_numpy()


def _np(s) -> np.ndarray:
    return pd.Series(s).fillna(False).astype(bool).to_numpy()


def _wrap(fn):
    """يضمن أن كل بوت يعيد مصفوفتين منطقيتين بالطول الصحيح، مهما كان داخلها."""
    def run(df):
        buy, sell = fn(df)
        return _np(buy), _np(sell)
    run.__name__ = fn.__name__
    return run


def close_momentum(df):
    """الإغلاق قرب أعلى الشمعة أو أدناها مع اتجاه الإغلاق نفسه."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    pos = (df["close"] - df["low"]) / rng
    move = df["close"] - df["close"].shift(1)
    return _np((pos >= 0.8) & (move > 0)), _np((pos <= 0.2) & (move < 0))


def xau_breakout_pro(df, n: int = 5):
    """اختراق قمة أو قاع آخر 5 شموع مع زخم الثلاث شموع الأخيرة."""
    close = df["close"]
    mom = close.pct_change(3)
    buy = _up(close, df["high"].rolling(n).max().shift(1)) & (mom > 0).to_numpy()
    sell = _dn(close, df["low"].rolling(n).min().shift(1)) & (mom < 0).to_numpy()
    return buy, sell


def two_candle_reversal(df):
    """انعكاس الشمعتين: شمعة صاعدة بعد هابطة للشراء، والعكس للبيع.
    مفهوم عام معروف في تحليل الشموع، وكُتب هنا من جديد."""
    o, c = df["open"], df["close"]
    bull = (c > o) & (c.shift(1) < o.shift(1))
    bear = (c < o) & (c.shift(1) > o.shift(1))
    return _np(bull), _np(bear)


BOT_FUNCS = {
    "close_momentum": close_momentum,
    "xau_breakout_pro": xau_breakout_pro,
    "two_candle_reversal": two_candle_reversal,
}
BOT_FUNCS = {k: _wrap(v) for k, v in BOT_FUNCS.items()}

BOT_AR = {
    "close_momentum": "بوت زخم الإغلاق",
    "xau_breakout_pro": "بوت اختراق الذهب الخاطف (5 شموع)",
    "two_candle_reversal": "بوت انعكاس الشمعتين",
}
