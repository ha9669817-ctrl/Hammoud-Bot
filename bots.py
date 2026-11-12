"""البوت الذكي: قواعد متعددة الفريمات (ساعة، 4 ساعات، يومي) مع قائمة حظر يتعلّم منها من خسائره.

التحليل يعتمد على شموع الساعة فقط، والفريمات الأعلى تُجمَّع منها.
الشمعة المكتملة فقط تُستخدم، فلا يوجد نظر إلى المستقبل.
قائمة الحظر BRAIN_BLOCKED يحدّثها المحرك من الصفقات المغلقة قبل كل فحص.
"""
import numpy as np
import pandas as pd

# سياقات (جهة + حالة الفريمات + RSI + التذبذب) خسرت مراراً، ولا يُدخل البوت فيها
BRAIN_BLOCKED: set = set()
BRAIN_NAME = "ai_brain"


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


def _aggregate(h1: pd.DataFrame, hours: int) -> pd.DataFrame:
    """يجمّع شموع الساعة إلى فريم أعلى، ويضيف وقت توفّر كل شمعة (بعد اكتمالها)."""
    g = h1.set_index("open_time").resample(f"{hours}h", label="left", closed="left")
    agg = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                        "low": g["low"].min(), "close": g["close"].last()}).dropna()
    agg = agg.reset_index()
    agg["avail"] = agg["open_time"] + pd.Timedelta(hours=hours)
    agg["trend"] = np.sign(_ema(agg["close"], 20) - _ema(agg["close"], 50)).astype(int)
    return agg[["avail", "trend"]]


def _higher_trend(h1: pd.DataFrame, hours: int) -> np.ndarray:
    """اتجاه آخر شمعة مكتملة للفريم الأعلى، لكل شمعة ساعة (بنفس ترتيب h1)."""
    agg = _aggregate(h1, hours).rename(columns={"avail": "t"}).sort_values("t")
    left = pd.DataFrame({"t": h1["open_time"].reset_index(drop=True), "i": np.arange(len(h1))}).sort_values("t")
    merged = pd.merge_asof(left, agg, on="t", direction="backward").sort_values("i")
    return merged["trend"].fillna(0).astype(int).to_numpy()


def brain_contexts(h1: pd.DataFrame):
    """يعيد (سياق الشراء، سياق البيع، إشارة الشراء الخام، إشارة البيع الخام) لكل شمعة ساعة."""
    close = h1["close"]
    ema20 = _ema(close, 20)
    trend_h1 = np.sign(ema20 - _ema(close, 50)).astype(int).to_numpy()
    trend_h4 = _higher_trend(h1, 4)
    trend_d1 = _higher_trend(h1, 24)
    rsi = _rsi(close, 14).fillna(50).to_numpy()
    atr = _atr(h1, 14)
    ratio = (atr / atr.rolling(60).median()).fillna(1.0).to_numpy()
    above = (close > ema20).to_numpy()
    below = (close < ema20).to_numpy()

    regime = np.where(ratio < 0.8, "low", np.where(ratio > 1.25, "high", "mid"))
    rsi_b = np.where(rsi < 45, "lo", np.where(rsi <= 70, "mid", "hi"))

    raw_buy = (trend_h1 == 1) & (trend_h4 == 1) & (trend_d1 == 1) & above & (rsi >= 45) & (rsi <= 70)
    raw_sell = (trend_h1 == -1) & (trend_h4 == -1) & (trend_d1 == -1) & below & (rsi >= 30) & (rsi <= 55)
    ctx_buy = np.char.add(np.char.add("buy|rsi", rsi_b), np.char.add("|atr", regime))
    ctx_sell = np.char.add(np.char.add("sell|rsi", rsi_b), np.char.add("|atr", regime))
    return ctx_buy, ctx_sell, raw_buy, raw_sell


def ai_brain(df: pd.DataFrame):
    """يدخل عند توافق الاتجاه على الفريمات الثلاثة، ويتجنب السياقات المحظورة.
    الإشارة تُحسب عند أول شمعة تحقق الشرط، لا في كل شمعة متتالية."""
    ctx_buy, ctx_sell, raw_buy, raw_sell = brain_contexts(df)
    ok_buy = raw_buy & ~np.isin(ctx_buy, list(BRAIN_BLOCKED))
    ok_sell = raw_sell & ~np.isin(ctx_sell, list(BRAIN_BLOCKED))
    buy = ok_buy & ~np.r_[False, ok_buy[:-1]]
    sell = ok_sell & ~np.r_[False, ok_sell[:-1]]
    return buy, sell


def _wrap(fn):
    """يضمن أن البوت يعيد مصفوفتين منطقيتين بالطول الصحيح."""
    def run(df):
        buy, sell = fn(df)
        return (pd.Series(buy).fillna(False).astype(bool).to_numpy(),
                pd.Series(sell).fillna(False).astype(bool).to_numpy())
    run.__name__ = fn.__name__
    return run


BOT_FUNCS = {BRAIN_NAME: _wrap(ai_brain)}
BOT_AR = {BRAIN_NAME: "البوت الذكي (متعدد الفريمات + تعلم من الخسائر)"}
