"""قواعد الدخول والوقف المتحرك (دوال نقية قابلة للاختبار).

الوقف لا يتحرك إلا لصالح الصفقة، ولا نغلق يدوياً: الإغلاق يتم بالوقف أو بالهدف النهائي فقط.
"""
PENDING_HOURS = 6
MAX_SL_DIST = 60.0
LOCK_R = 0.10              # بعد TP1 يُنقل الوقف إلى الدخول + 0.1R (ربح صغير مضمون)
# الهدف النهائي = آخر هدف في التوصية؛ الوقف المتحرك يؤمّن ما قبله تدريجياً


KEEP_FRAC = 0.6   # بعد ربح 10$ يُؤمَّن 60% من أعلى ربح على الأقل (10$ ← +6$، 28$ ← +16.8$)
MIN_SL_ROOM = 1.0          # أقل مسافة بين سعر الدخول والوقف كي نقبل الدخول الفوري


def plan_entry(sig: dict, price: float, chase: bool = False):
    """يعيد ("market", سعر) أو ("pending", حافة المنطقة) أو ("skip", سبب).

    - داخل المنطقة، أو السعر أفضل منها (شراء تحتها / بيع فوقها): دخول فوري بسعر السوق.
    - لم يصل السعر للمنطقة بعد (شراء فوقها / بيع تحتها): انتظار لمس حافتها مهما بعدت (أمر معلّق داخلي).
    """
    side, lo, hi, sl = sig["side"], sig["lo"], sig["hi"], sig["sl"]
    if lo is None:
        kind, entry = "market", price
    elif lo <= price <= hi:
        kind, entry = "market", price
    elif side == "buy":
        kind, entry = ("pending", hi) if price > hi else ("market", price)
    else:
        kind, entry = ("pending", lo) if price < lo else ("market", price)
    if kind == "pending" and chase:
        kind, entry = "market", price     # السعر لمس منطقة الدخول سابقاً ثم ابتعد: نلحقه بسعر السوق
    if kind == "market":
        room = (entry - sl) if side == "buy" else (sl - entry)
        if room < MIN_SL_ROOM:
            return "skip", "السعر وصل الوقف أو تجاوزه فلا مجال للدخول"
    risk = abs(entry - sl)
    if risk <= 0 or risk > MAX_SL_DIST:
        return "skip", f"مسافة الوقف غير منطقية ({risk:.1f}$)"
    return kind, entry


def final_target(sig: dict):
    """الهدف النهائي، أو None إن لم تحدد التوصية أهدافاً (يُغلق الوقف المتحرك الصفقة وحده)."""
    tps = sig["tps"]
    return tps[-1] if tps else None


def next_stop(side: str, entry: float, sl0: float, tps: list, current_stop: float,
              high: float, low: float, level: int):
    """يحسب الوقف الجديد بعد شمعة (high/low). يعيد (stop, level) ولا يتراجع أبداً."""
    risk = abs(entry - sl0)
    stop = current_stop
    n = len(tps)
    for k in range(n):
        reached = high >= tps[k] if side == "buy" else low <= tps[k]
        if not reached or k + 1 <= level:
            continue
        level = k + 1
        if k == 0:
            cand = entry + (LOCK_R * risk if side == "buy" else -LOCK_R * risk)
        else:
            cand = tps[k - 1]
        better = cand > stop if side == "buy" else cand < stop
        if better:
            stop = cand
    return stop, level


def is_better_stop(side: str, old: float, new: float) -> bool:
    return new > old if side == "buy" else new < old


# ---- وقف الأرباح بالدولار (يعمل بلا أهداف). الأرقام لكل 0.01 لوت وتتناسب مع حجم الصفقة.
USD_ARM = 5.0      # عند ربح 5$ يُؤمَّن الدخول
USD_BE_LOCK = 1.0  # ويُحجز 1$ ربح لتغطية التكلفة
USD_START = 10.0   # عند ربح 10$ يبدأ الوقف يلحق السعر
USD_GAP = 5.0      # ويبقى بعيداً عن أعلى ربح وصلته الصفقة بمقدار 5$
MIN_LOT = 0.01


def dollar_stop(side: str, entry: float, best: float, usd_per_price: float, lots: float, trail: bool = True):
    """الوقف المطلوب لأفضل سعر وصلته الصفقة، أو None قبل بلوغ حد التأمين.
    trail=False: تأمين الدخول فقط (+1$) دون الملاحقة؛ الملاحقة عندئذ للتحليل الهيكلي."""
    if not usd_per_price or usd_per_price <= 0 or not lots:
        return None
    k = max(lots, MIN_LOT) / MIN_LOT
    gain = (best - entry) if side == "buy" else (entry - best)
    profit = gain * usd_per_price
    if profit < USD_ARM * k:
        return None
    lock = profit - USD_GAP * k if (trail and profit >= USD_START * k) else USD_BE_LOCK * k
    dist = lock / usd_per_price
    return entry + dist if side == "buy" else entry - dist


def stop_valid(side: str, stop: float, price) -> bool:
    """الوقف يجب أن يكون خلف السعر الحالي وإلا يُضرب فوراً أو يرفضه الوسيط."""
    if price is None:
        return True
    return stop < price if side == "buy" else stop > price


def smart_stop(side, entry, best, usd_per_price, lots, bars):
    """وقف ذكي بعد ربح 10$: يعتمد هيكل M5 (تصحيح/اتجاه ← واسع، انعكاس ← ضيق). يعيد (stop, state) أو (None, None)."""
    from . import structure
    if not usd_per_price or not lots or not bars:
        return None, None
    k = max(lots, MIN_LOT) / MIN_LOT
    gain = (best - entry) if side == "buy" else (entry - best)
    if gain * usd_per_price < USD_START * k:
        return None, None
    a = structure.analyze(side, entry, best, bars)
    if not a:
        return None, None
    sign = 1 if side == "buy" else -1
    floor = entry + (USD_BE_LOCK * k / usd_per_price) * sign
    # سقف التراجع: الوقف الهيكلي يتسع مع ATR (على ذهب متقلب قد يكون 20$+ خلف القمة فلا يتحرك شيء بين +5 و+25).
    # لذلك لا نسمح بأن يتنازل الوقف عن أكثر من (1-KEEP) من أعلى ربح بلغته الصفقة.
    keep = entry + KEEP_FRAC * gain * sign
    best_stop = max(a["stop"], floor, keep) if side == "buy" else min(a["stop"], floor, keep)
    return best_stop, a["state"]
