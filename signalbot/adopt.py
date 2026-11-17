"""إدارة الصفقات المفتوحة يدوياً: أي مركز تفتحه أنت يُكتشف ويُدار وقفه المتحرك تلقائياً.

القاعدة (بدون أهداف جاهزة): R = المسافة بين الدخول والوقف الأصلي.
عند +1R يُنقل الوقف إلى الدخول+0.1R، وعند +kR (k≥2) إلى الدخول+(k-1)R. لا يرجع الوقف أبداً، ولا نغلق يدوياً،
ولا نمس الهدف. مركز بلا وقف لا يمكن حساب R له فيُسجَّل ويُترك.
"""
from . import manage


def _pick(d, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def normalize(p):
    side = _pick(p, "side", "type", "direction")
    if isinstance(side, str):
        side = "buy" if "BUY" in side.upper() else "sell" if "SELL" in side.upper() else None
    elif side in (0, 1):
        side = "buy" if side == 0 else "sell"
    else:
        side = None
    try:
        entry = float(_pick(p, "openPrice", "open_price", "priceOpen", "price_open", "entryPrice", "price"))
    except (TypeError, ValueError):
        return None
    ticket = _pick(p, "ticket", "id", "positionId")
    sl = _pick(p, "stopLoss", "stop_loss", "sl")
    sl = float(sl) if sl not in (None, 0, "0", "") else None
    if side is None or ticket is None:
        return None
    return {"ticket": ticket, "symbol": p.get("symbol"), "side": side, "entry": entry, "sl": sl}


def target_stop(side, entry, sl0, best):
    """الوقف المطلوب لأفضل سعر وصله المركز (best)، أو None إن لم يبلغ +1R."""
    risk = abs(entry - sl0)
    if risk <= 0:
        return None
    gain = (best - entry) if side == "buy" else (entry - best)
    k = int(gain // risk)
    if k < 1:
        return None
    r = manage.LOCK_R if k == 1 else k - 1
    return entry + r * risk if side == "buy" else entry - r * risk


class ManualManager:
    def __init__(self, executor, journal):
        self.ex, self.j = executor, journal
        self.state = journal.load_state().get("adopt", {})   # ticket -> {sl0, best, stop, since, ...}
        self.warned = set()

    def tick(self, exclude, now_ts):
        seen = set()
        for pos in (normalize(p) for p in self.ex.positions_raw()):
            if pos is None or str(pos["ticket"]) in exclude:
                continue
            tk = str(pos["ticket"])
            seen.add(tk)
            if pos["sl"] is None:
                if tk not in self.warned:
                    self.warned.add(tk)
                    self.j.event("adopt_no_sl", ticket=pos["ticket"], note="مركز بلا وقف: لا يمكن حساب R")
                continue
            st = self.state.get(tk)
            if st is None:
                st = self.state[tk] = {"sl0": pos["sl"], "best": pos["entry"], "stop": pos["sl"],
                                       "since": now_ts, "symbol": pos["symbol"], "side": pos["side"],
                                       "entry": pos["entry"]}
                self.j.event("adopt", ticket=pos["ticket"], symbol=pos["symbol"], side=pos["side"],
                             entry=pos["entry"], sl=pos["sl"])
            if manage.is_better_stop(pos["side"], st["stop"], pos["sl"]):
                st["stop"] = pos["sl"]
            bars, last_ts, close = self.ex.bars_for(pos["symbol"], st["since"])
            for hi, lo in bars:
                st["best"] = max(st["best"], hi) if pos["side"] == "buy" else min(st["best"], lo)
            if last_ts:
                st["since"] = last_ts
            new = target_stop(pos["side"], st["entry"], st["sl0"], st["best"])
            if new is None or not manage.is_better_stop(pos["side"], st["stop"], new):
                continue
            if close is not None and ((pos["side"] == "buy" and new >= close)
                                      or (pos["side"] == "sell" and new <= close)):
                continue  # السعر رجع عن المستوى: لا نضع وقفاً يُضرب فوراً
            try:
                self.ex.modify_sl(pos["ticket"], new)
            except Exception as e:
                self.j.event("error", where="adopt_modify", ticket=pos["ticket"], error=str(e))
                continue
            self.j.event("trail", ticket=pos["ticket"], old=st["stop"], new=new, manual=True)
            st["stop"] = new
        for tk in list(self.state):
            if tk not in seen and tk not in exclude:
                del self.state[tk]       # أُغلق المركز
        st_all = self.j.load_state()
        st_all["adopt"] = self.state
        self.j.save_state(st_all)
