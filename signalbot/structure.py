"""تحليل هيكل السعر لتحديد الوقف: تصحيح (نكمل) أم انعكاس (نؤمّن)؟

هذا فحص قواعد على شموع M5، وليس تنبؤاً بالمستقبل: يقيس عمق التراجع عن القمة وهل انكسر آخر قاع (للشراء)
ويضبط مسافة الوقف: واسعة مع التصحيح العادي حتى لا يُضرب الوقف في إعادة الاختبار، وضيقة عند كسر الهيكل.
الشموع: قائمة (high, low, close) من الأقدم للأحدث، وآخر شمعة هي الجارية (لا تدخل في الهيكل).
"""
FRACTAL_N = 2          # شمعتان على كل جانب لتأكيد القاع/القمة
ATR_N = 14
MIN_BARS = 25
RETR_PULLBACK = 0.236  # أقل من هذا = اتجاه نظيف
RETR_REVERSAL = 0.618  # أعمق من هذا = انعكاس محتمل
BREAK_BUFFER = 0.1     # كسر القاع بأكثر من 0.1 ATR
ROOM_MIN = 1.5         # أقرب مسافة للوقف خلف القمة (ATR): تحمّل إعادة الاختبار
ROOM_MAX = 3.5         # أبعد مسافة (ATR): لا نتنازل عن ربح كثير
SWING_BUFFER = 0.3     # تحت القاع بـ0.3 ATR
TIGHT = 0.7            # عند الانعكاس نقترب إلى 0.7 ATR


def usable(bars):
    """هل الشموع كافية للتحليل الهيكلي؟ وإلا تعمل الملاحقة بالدولار بدلاً منه (لا فراغ بينهما)."""
    return bool(bars) and len(bars) >= MIN_BARS


def atr(bars, n=ATR_N):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, _ = bars[i]
        pc = bars[i - 1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def last_swing_low(bars, below):
    """أحدث قاع مؤكّد (فراكتل) أدنى من السعر `below`. يعيد (index, price) أو None."""
    n = FRACTAL_N
    for i in range(len(bars) - 1 - n, n - 1, -1):
        low = bars[i][1]
        if low >= below:
            continue
        if all(low < bars[i - k][1] for k in range(1, n + 1)) and all(low <= bars[i + k][1] for k in range(1, n + 1)):
            return i, low
    return None


def _buy(entry, best, bars):
    if len(bars) < MIN_BARS:
        return None
    closed = bars[:-1]
    a = atr(closed)
    if not a or a <= 0:
        return None
    last_close = bars[-1][2]
    sw = last_swing_low(closed, below=best)
    swing = sw[1] if sw else entry
    leg = best - swing
    retr = (best - last_close) / leg if leg > 0 else 0.0
    broke = last_close < swing - BREAK_BUFFER * a
    if broke or retr > RETR_REVERSAL:
        state = "reversal"
        stop = best - TIGHT * a
    else:
        state = "trend" if retr <= RETR_PULLBACK else "pullback"
        stop = max(swing - SWING_BUFFER * a, best - ROOM_MAX * a)
        stop = min(stop, best - ROOM_MIN * a)
    return {"state": state, "stop": stop, "atr": a, "swing": swing, "retr": round(retr, 3)}


def analyze(side, entry, best, bars):
    """يعيد {state, stop, atr, swing, retr} أو None إن لم تكفِ الشموع. البيع بالقلب (سالب الأسعار)."""
    if side == "buy":
        return _buy(entry, best, bars)
    flipped = [(-l, -h, -c) for h, l, c in bars]
    r = _buy(-entry, -best, flipped)
    if r is None:
        return None
    return {**r, "stop": -r["stop"], "swing": -r["swing"]}
