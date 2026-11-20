"""استراتيجية «ارتداد مع الاتجاه» متعددة الفريمات للذهب. لا تدخل أبداً ضد اتجاه الفريمات الأكبر.

الاتجاه: H4 وH1 وM15 يجب أن تتفق (EMA سريع/بطيء وميل). الدخول: تراجع إلى EMA21 على M5 ثم إغلاق شمعة
تعود في اتجاه الترند مع RSI يخرج من منطقة التراجع. فلتر ADX على H1 لتجنب السوق العرضي.
"""
import numpy as np
import pandas as pd

from . import indicators as ind

DEFAULTS = dict(adx_min=20.0, rsi_buy=45.0, rsi_sell=55.0, atr_sl=1.5, min_atr=0.8, max_atr=6.0)
# الإعداد المعتمد للاختراق (الأوسط من شبكة الاختبار، وليس الأفضل في التدريب لتفادي التحيّز)
LIVE = dict(mode="breakout", adx_min=20.0, atr_sl=3.0, max_atr=9.0)


def _trend(df_tf, fast, slow):
    """+1 صاعد، -1 هابط، 0 غير واضح؛ مُزاح شمعة واحدة كي لا نستخدم شمعة لم تكتمل."""
    f, s = ind.ema(df_tf["close"], fast), ind.ema(df_tf["close"], slow)
    t = pd.Series(0, index=df_tf.index)
    t[(f > s) & (f > f.shift(3)) & (df_tf["close"] > s)] = 1
    t[(f < s) & (f < f.shift(3)) & (df_tf["close"] < s)] = -1
    return t.shift(1)


def prepare(m5, h1=None, h4=None, m15=None):
    """يضيف أعمدة الاتجاه والمؤشرات إلى M5. m5: فهرس زمني UTC وأعمدة open/high/low/close.
    في الاختبار التاريخي تُشتق الفريمات الأكبر من M5؛ وفي التشغيل الحي تُمرَّر مسحوبة من الوسيط (تاريخ أطول للـEMA200)."""
    d = m5.copy()
    h1 = h1 if h1 is not None else ind.resample(d, "1h")
    h4 = h4 if h4 is not None else ind.resample(d, "4h")
    m15 = m15 if m15 is not None else ind.resample(d, "15min")
    cols = {"t_h4": _trend(h4, 20, 50), "t_h1": _trend(h1, 50, 200), "t_m15": _trend(m15, 21, 50),
            "adx_h1": ind.adx(h1).shift(1)}
    for k, v in cols.items():
        d[k] = v.reindex(d.index, method="ffill")
    d["ema21"] = ind.ema(d["close"], 21)
    d["rsi"] = ind.rsi(d["close"])
    d["atr"] = ind.atr(d)
    d["dch_hi"] = d["high"].rolling(48).max().shift(1)     # قمة آخر 4 ساعات (48 شمعة M5) قبل الشمعة الحالية
    d["dch_lo"] = d["low"].rolling(48).min().shift(1)
    return d


def decide(d, i, **kw):
    """قرار عند إغلاق الشمعة i: 'buy' / 'sell' / None مع المسافة المقترحة للوقف. لا يستعمل بيانات بعدها."""
    p = {**DEFAULTS, **kw}
    r, pr = d.iloc[i], d.iloc[i - 1]
    if any(pd.isna(r[c]) for c in ("t_h4", "t_h1", "t_m15", "adx_h1", "ema21", "rsi", "atr")):
        return None
    if not (p["min_atr"] <= r["atr"] <= p["max_atr"]) or r["adx_h1"] < p["adx_min"]:
        return None
    trend = r["t_h4"] + r["t_h1"] + r["t_m15"]
    if p.get("mode") == "breakout":
        if trend == 3 and r["close"] > r["dch_hi"] and pr["close"] <= pr["dch_hi"]:
            return "buy", p["atr_sl"] * r["atr"]
        if trend == -3 and r["close"] < r["dch_lo"] and pr["close"] >= pr["dch_lo"]:
            return "sell", p["atr_sl"] * r["atr"]
        return None
    if trend == 3:      # الثلاثة صاعدة
        pulled = pr["low"] <= pr["ema21"] or d["low"].iloc[i - 3:i].min() <= d["ema21"].iloc[i - 3:i].min()
        if pulled and r["close"] > r["ema21"] and r["close"] > r["open"] and pr["rsi"] <= p["rsi_buy"] + 10 and r["rsi"] > pr["rsi"]:
            return "buy", p["atr_sl"] * r["atr"]
    if trend == -3:
        pulled = pr["high"] >= pr["ema21"] or d["high"].iloc[i - 3:i].max() >= d["ema21"].iloc[i - 3:i].max()
        if pulled and r["close"] < r["ema21"] and r["close"] < r["open"] and pr["rsi"] >= p["rsi_sell"] - 10 and r["rsi"] < pr["rsi"]:
            return "sell", p["atr_sl"] * r["atr"]
    return None
