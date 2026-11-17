"""قواعد الدخول والوقف المتحرك (دوال نقية قابلة للاختبار).

الوقف لا يتحرك إلا لصالح الصفقة، ولا نغلق يدوياً: الإغلاق يتم بالوقف أو بالهدف النهائي فقط.
"""
MAX_PENDING_DIST = 5.0     # دولار: أبعد من هذا عن المنطقة = تجاهل
PENDING_HOURS = 6
MAX_SL_DIST = 60.0
LOCK_R = 0.10              # بعد TP1 يُنقل الوقف إلى الدخول + 0.1R (ربح صغير مضمون)
FINAL_TP_INDEX = 2         # الهدف النهائي TP3


def plan_entry(sig: dict, price: float):
    """يعيد ("market", entry) أو ("pending", edge) أو ("skip", سبب)."""
    side, lo, hi, sl = sig["side"], sig["lo"], sig["hi"], sig["sl"]
    if lo is None:
        lo = hi = price
    if lo <= price <= hi:
        entry, kind = price, "market"
    else:
        dist = (lo - price) if price < lo else (price - hi)
        if dist > MAX_PENDING_DIST:
            return "skip", f"السعر بعيد عن المنطقة بمقدار {dist:.1f}$"
        # السعر تحت المنطقة: شراء يحتاج Buy Stop؟ نقتصر على الحالة الطبيعية: ننتظر لمس الحافة بأمر معلّق
        entry, kind = (lo if price < lo else hi), "pending"
    risk = abs(entry - sl)
    if risk <= 0 or risk > MAX_SL_DIST:
        return "skip", f"مسافة الوقف غير منطقية ({risk:.1f}$)"
    return kind, entry


def final_target(sig: dict):
    """الهدف النهائي، أو None إن لم تحدد التوصية أهدافاً (يُغلق الوقف المتحرك الصفقة وحده)."""
    tps = sig["tps"]
    return tps[min(FINAL_TP_INDEX, len(tps) - 1)] if tps else None


def next_stop(side: str, entry: float, sl0: float, tps: list, current_stop: float,
              high: float, low: float, level: int):
    """يحسب الوقف الجديد بعد شمعة (high/low). يعيد (stop, level) ولا يتراجع أبداً."""
    risk = abs(entry - sl0)
    stop = current_stop
    n = min(len(tps), FINAL_TP_INDEX + 1)
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


def dollar_stop(side: str, entry: float, best: float, usd_per_price: float, lots: float):
    """الوقف المطلوب لأفضل سعر وصلته الصفقة، أو None قبل بلوغ حد التأمين."""
    if not usd_per_price or usd_per_price <= 0 or not lots:
        return None
    k = max(lots, MIN_LOT) / MIN_LOT
    gain = (best - entry) if side == "buy" else (entry - best)
    profit = gain * usd_per_price
    if profit < USD_ARM * k:
        return None
    lock = profit - USD_GAP * k if profit >= USD_START * k else USD_BE_LOCK * k
    dist = lock / usd_per_price
    return entry + dist if side == "buy" else entry - dist


def stop_valid(side: str, stop: float, price) -> bool:
    """الوقف يجب أن يكون خلف السعر الحالي وإلا يُضرب فوراً أو يرفضه الوسيط."""
    if price is None:
        return True
    return stop < price if side == "buy" else stop > price
