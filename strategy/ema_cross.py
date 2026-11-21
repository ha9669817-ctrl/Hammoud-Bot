"""استراتيجية التقاطع كما وصفها المستخدم حرفياً.

الاتجاه: السعر فوق EMA200 ← شراء فقط، تحته ← بيع فقط. الدخول: تقاطع EMA9 فوق/تحت EMA21 عند إغلاق الشمعة.
الوقف: أقرب قاع (شراء) / قمة (بيع) من آخر 10 شموع، وإن كانت المسافة <0.5 أو >4 ATR فبديل 1.5×ATR.
الهدف: 1:2 (ضعف مسافة الوقف). لا ملاحقة: الوقف والهدف ثابتان كما في الوصف.
"""
from . import indicators as ind

SWING_BARS = 10


def decide(df, i):
    """قرار عند إغلاق الشمعة i (فهرس موضعي). يعيد (side, sl, tp, entry_ref) أو None. لا يستعمل ما بعد i."""
    if i < 205:
        return None
    c = df["close"]
    e9, e21, e200, a = ind.ema(c, 9), ind.ema(c, 21), ind.ema(c, 200), ind.atr(df)
    price, atr = float(c.iloc[i]), float(a.iloc[i])
    if not atr > 0:
        return None
    up = price > e200.iloc[i] and e9.iloc[i - 1] <= e21.iloc[i - 1] and e9.iloc[i] > e21.iloc[i]
    dn = price < e200.iloc[i] and e9.iloc[i - 1] >= e21.iloc[i - 1] and e9.iloc[i] < e21.iloc[i]
    if not (up or dn):
        return None
    side = "buy" if up else "sell"
    win = df.iloc[i - SWING_BARS + 1: i + 1]
    sl = float(win["low"].min()) if up else float(win["high"].max())
    dist = abs(price - sl)
    if dist < 0.5 * atr or dist > 4 * atr:
        dist = 1.5 * atr
        sl = price - dist if up else price + dist
    tp = price + 2 * dist if up else price - 2 * dist
    return side, sl, tp, price
