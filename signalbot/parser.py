"""تحويل نص رسالة توصية إلى إشارة منظمة. أي رسالة غير مكتملة تُرفض (لا تخمين)."""
import re

NUM = r"(\d{3,5}(?:\.\d+)?)"
GOLD_MIN, GOLD_MAX = 1000.0, 9000.0


def _valid(side, lo, hi, sl, tps):
    ref = [x for x in [sl] + tps + ([lo, hi] if lo is not None else [])]
    if not all(GOLD_MIN < x < GOLD_MAX for x in ref):
        return False
    if side == "buy":
        if lo is not None and not (sl < lo and (not tps or tps[0] > hi)):
            return False
        if lo is None and tps and not tps[0] > sl:
            return False
    else:
        if lo is not None and not (sl > hi and (not tps or tps[0] < lo)):
            return False
        if lo is None and tps and not tps[0] < sl:
            return False
    return True


def _structured(s):
    """صيغة: XAUUSD BUY / ENTRY lo - hi / SL / TP1.. (القنوات المنظمة وLiquidity Trader)."""
    m = re.search(r"\b(BUY|SELL)\b", s)
    if not m:
        return None
    side = "buy" if m.group(1) == "BUY" else "sell"
    after = s[m.end():]
    e = re.search(r"ENTRY[^\d]*" + NUM + r"[^\d]+" + NUM, after) or \
        re.search(r"(?:GOLD|XAUUSD)[^\d\n]*" + NUM + r"\s*[/\-–]\s*" + NUM, after)
    if e:
        e1, e2 = float(e.group(1)), float(e.group(2))
    else:
        e1m = re.search(r"ENTRY[^\d]*" + NUM, after) or re.search(r"(?:GOLD|XAUUSD)[^\d\n]*" + NUM, after)
        if not e1m:
            return None
        e1 = e2 = float(e1m.group(1))
    sl_m = re.search(r"\bSL\b[^\d\n]*" + NUM, after) or re.search(r"STOP[^\d\n]*LOSS[^\d\n]*" + NUM, after)
    if not sl_m:
        return None
    tps = [float(x) for x in re.findall(r"(?<![A-Z])TP\s*\d*[^\d\n]*" + NUM, after)]
    return side, min(e1, e2), max(e1, e2), float(sl_m.group(1)), tps


def _one_liner(s):
    """صيغة Master blue: «Gold buy now 4420-4422» أو «Gold sell now» (بلا سعر = سوق)."""
    first = s.strip().split("\n")[0]
    m = re.search(r"\b(BUY|SELL)\b", first)
    if not m:
        return None
    side = "buy" if m.group(1) == "BUY" else "sell"
    rng = re.search(NUM + r"\s*[-–/]\s*" + NUM, first[m.end():])
    single = re.search(NUM, first[m.end():])
    if rng:
        lo, hi = sorted([float(rng.group(1)), float(rng.group(2))])
    elif single:
        lo = hi = float(single.group(1))
    else:
        lo = hi = None
    sl_m = re.search(r"SL[:\s]*" + NUM, s)
    if not sl_m:
        return None
    tps = [float(x) for x in re.findall(r"(?<![A-Z])TP\s*\d*[:\s]*" + NUM, s)]
    return side, lo, hi, float(sl_m.group(1)), tps


def _first_line(s):
    for ln in s.splitlines():
        if ln.strip():
            return ln
    return ""


# قواعد خاصة بكل قناة: السطر الأول يجب أن يحدد الذهب (حتى لا تُقرأ توصيات أزواج أخرى أو ملخصات)
CHANNEL_HEAD = {
    "free forex signals": r"(^|\n)\s*(BUY|SELL)(\s+(LIMIT|STOP))?\s+(GOLD|XAUUSD)\b",
    "master blue": r"\b(GOLD|XAUUSD)\b.*\b(BUY|SELL)\b|\b(BUY|SELL)\b.*\b(GOLD|XAUUSD)\b",
    "liquidity trader": r"\b(BUY|SELL)\b.*\b(GOLD|XAUUSD)\b|\b(GOLD|XAUUSD)\b.*\b(BUY|SELL)\b",
    "trade sniper": r"XAUUSD|GOLD",
}


def parse_signal(text: str, channel: str = None):
    """يعيد dict(side, lo, hi, sl, tps) أو None. lo/hi=None تعني دخول بسعر السوق."""
    s = re.sub(r"[*_`]", "", (text or "")).upper()
    if not re.search(r"XAU|GOLD", s):
        return None
    head = CHANNEL_HEAD.get((channel or "").strip().lower())
    if head and not re.search(head, _first_line(s) if channel.lower() != "free forex signals" else "\n".join(s.splitlines()[:3])):
        return None
    for fn in (_structured, _one_liner):
        r = fn(s)
        if r and _valid(*r):
            side, lo, hi, sl, tps = r
            return {"side": side, "lo": lo, "hi": hi, "sl": sl, "tps": tps}
    return None
