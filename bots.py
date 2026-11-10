"""كتالوج البوتات: منطق كل بوت مكتوب هنا بالكامل بناءً على مفاهيم تحليل فنية معروفة.
لم يُنسخ أي سطر من مستودعات طرف ثالث.

كل بوت دالة نقية: تأخذ شموع الأصل وتعيد (شراء، بيع) كمصفوفتين منطقيتين.
الإشارة تُحسب عند إغلاق الشمعة، والدخول بعدها.

ملاحظة: بوت شبكة السحب العكسية (Grid) غير مُدرج، لأنه يحتاج إدارة عدة صفقات متزامنة
وليس إشارة دخول واحدة، ولا يناسب هذا المحرك.
"""
import numpy as np
import pandas as pd


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _up(a: pd.Series, b) -> np.ndarray:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return ((a > b) & (a.shift(1) <= b.shift(1))).fillna(False).to_numpy()


def _dn(a: pd.Series, b) -> np.ndarray:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return ((a < b) & (a.shift(1) >= b.shift(1))).fillna(False).to_numpy()


def _np(s) -> np.ndarray:
    return pd.Series(s).fillna(False).astype(bool).to_numpy()


def ema_macd_scalp(df):
    """تقاطع EMA 9/21 مع اتجاه MACD في الاتجاه نفسه."""
    close = df["close"]
    e9, e21 = _ema(close, 9), _ema(close, 21)
    macd = _ema(close, 12) - _ema(close, 26)
    hist = macd - _ema(macd, 9)
    return _np(_up(e9, e21) & (hist > 0)), _np(_dn(e9, e21) & (hist < 0))


def bb_rsi_reversion(df):
    """ارتداد من حدود بولنجر، مؤكَّد بخروج RSI من التشبع."""
    close = df["close"]
    mid = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    lo, up = mid - 2 * sd, mid + 2 * sd
    r = _rsi(close, 14)
    buy = _up(r, 30) & (df["low"].shift(1) < lo.shift(1)).to_numpy()
    sell = _dn(r, 70) & (df["high"].shift(1) > up.shift(1)).to_numpy()
    return _np(buy), _np(sell)


def donchian_breakout(df, n: int = 20):
    """اختراق أعلى قمة أو أدنى قاع لآخر n شمعة."""
    close = df["close"]
    return (_up(close, df["high"].rolling(n).max().shift(1)),
            _dn(close, df["low"].rolling(n).min().shift(1)))


def vwap_momentum(df, n: int = 48):
    """عبور السعر لمتوسط VWAP المتحرك مع اتجاه الشمعة نفسها."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"]
    vwap = (tp * vol).rolling(n).sum() / vol.rolling(n).sum().replace(0, np.nan)
    close, op = df["close"], df["open"]
    return _np(_up(close, vwap) & (close > op)), _np(_dn(close, vwap) & (close < op))


def ema_crossover(df):
    """تقاطع EMA 9 مع EMA 21 لبداية الاتجاه."""
    close = df["close"]
    return _up(_ema(close, 9), _ema(close, 21)), _dn(_ema(close, 9), _ema(close, 21))


def rsi_extreme(df):
    """خروج RSI من التشبع الشرائي أو البيعي."""
    r = _rsi(df["close"], 14)
    return _up(r, 30), _dn(r, 70)


def fib_retracement(df, n: int = 50):
    """ارتداد من مستوى فيبوناتشي 0.618 داخل نطاق آخر n شمعة."""
    hh, ll = df["high"].rolling(n).max(), df["low"].rolling(n).min()
    rng = hh - ll
    close = df["close"]
    ok = (rng > 0).to_numpy()
    buy = _up(close, hh - 0.618 * rng) & ok
    sell = _dn(close, ll + 0.618 * rng) & ok
    return buy, sell


def false_breakout(df, n: int = 20):
    """اختراق وهمي: يتجاوز القمة أو القاع ثم يغلق داخله في الشمعة نفسها."""
    ph = df["high"].rolling(n).max().shift(1)
    pl = df["low"].rolling(n).min().shift(1)
    sell = ((df["high"] > ph) & (df["close"] < ph)).fillna(False).to_numpy()
    buy = ((df["low"] < pl) & (df["close"] > pl)).fillna(False).to_numpy()
    return buy, sell


def volume_spike(df, n: int = 20, mult: float = 2.0):
    """شمعة بحجم أعلى من ضعف المتوسط، والاتجاه مع لون الشمعة."""
    avg = df["volume"].rolling(n).mean().shift(1)
    spike = (df["volume"] > mult * avg).to_numpy()
    buy = spike & (df["close"] > df["open"]).to_numpy()
    sell = spike & (df["close"] < df["open"]).to_numpy()
    return _np(buy), _np(sell)


def daily_pivot(df):
    """نقطة الارتكاز اليومية (من يوم أمس) وعبور السعر لها."""
    days = df["open_time"].dt.date
    daily = df.assign(_d=days).groupby("_d").agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
    pivot_prev = ((daily["h"] + daily["l"] + daily["c"]) / 3).shift(1)
    p_bar = pd.Series(days.map(pivot_prev).astype(float).to_numpy())
    close = pd.Series(df["close"].to_numpy())
    return _np(_up(close, p_bar)), _np(_dn(close, p_bar))


def squeeze_breakout(df, n: int = 20):
    """ضغط تقلب (ATR تحت وسيطه) ثم اختراق القمة أو القاع."""
    a = _atr(df, 14)
    squeeze = (a < a.rolling(50).median()).shift(1).fillna(False).to_numpy()
    close = df["close"]
    buy = _up(close, df["high"].rolling(n).max().shift(1)) & squeeze
    sell = _dn(close, df["low"].rolling(n).min().shift(1)) & squeeze
    return buy, sell


def atr_breakout(df):
    """حركة الإغلاق أكبر من ATR واحد في اتجاه واحد لأول مرة."""
    a = _atr(df, 14)
    move = df["close"] - df["close"].shift(1)
    up = move > a
    dn = move < -a
    return _np(up & ~up.shift(1).fillna(False)), _np(dn & ~dn.shift(1).fillna(False))


def consecutive_candles(df, k: int = 3):
    """ثلاث شموع متتالية في اتجاه واحد، تُحسب لأول مرة فقط."""
    up = (df["close"] > df["open"]).astype(int)
    dn = (df["close"] < df["open"]).astype(int)
    all_up = up.rolling(k).sum() == k
    all_dn = dn.rolling(k).sum() == k
    return (_np(all_up & ~all_up.shift(1).fillna(False)),
            _np(all_dn & ~all_dn.shift(1).fillna(False)))


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


def _wrap(fn):
    """يضمن أن كل بوت يعيد مصفوفتين منطقيتين بالطول الصحيح، مهما كان داخلها."""
    def run(df):
        buy, sell = fn(df)
        return _np(buy), _np(sell)
    run.__name__ = fn.__name__
    return run


BOT_FUNCS = {
    "ema_macd_scalp": ema_macd_scalp,
    "bb_rsi_reversion": bb_rsi_reversion,
    "donchian": donchian_breakout,
    "vwap_momentum": vwap_momentum,
    "ema_crossover": ema_crossover,
    "rsi_extreme": rsi_extreme,
    "fib_retracement": fib_retracement,
    "false_breakout": false_breakout,
    "volume_spike": volume_spike,
    "daily_pivot": daily_pivot,
    "squeeze_breakout": squeeze_breakout,
    "atr_breakout": atr_breakout,
    "consecutive_candles": consecutive_candles,
    "close_momentum": close_momentum,
    "xau_breakout_pro": xau_breakout_pro,
}
BOT_FUNCS = {k: _wrap(v) for k, v in BOT_FUNCS.items()}

BOT_AR = {
    "ema_macd_scalp": "بوت السكالبينج السريع (EMA و MACD)",
    "bb_rsi_reversion": "بوت ارتداد بولنجر (مع RSI)",
    "donchian": "بوت اختراق القمم (دونشيان 20)",
    "vwap_momentum": "بوت الزخم والسيولة (VWAP)",
    "ema_crossover": "بوت تقاطع المتوسطات الأسي (9/21)",
    "rsi_extreme": "بوت القوة النسبية (RSI)",
    "fib_retracement": "بوت تصحيح فيبوناتشي (0.618)",
    "false_breakout": "بوت إيقاف الاختراق الكاذب",
    "volume_spike": "بوت التذبذب الحجمي",
    "daily_pivot": "بوت نقاط الارتكاز اليومية",
    "squeeze_breakout": "بوت اختراق التضيق",
    "atr_breakout": "بوت اختراق ATR",
    "consecutive_candles": "بوت الشموع المتتالية",
    "close_momentum": "بوت زخم الإغلاق",
    "xau_breakout_pro": "بوت اختراق الذهب الخاطف (5 شموع)",
}
