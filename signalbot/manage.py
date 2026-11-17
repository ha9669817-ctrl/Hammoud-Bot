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


def final_target(sig: dict) -> float:
    tps = sig["tps"]
    return tps[min(FINAL_TP_INDEX, len(tps) - 1)]


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
