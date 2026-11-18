"""إدارة الصفقات المفتوحة يدوياً: أي مركز تفتحه أنت يُكتشف ويُدار وقفه المتحرك تلقائياً.

القاعدة بالدولار (لكل 0.01 لوت، وتتناسب مع الحجم): عند ربح 5$ يُؤمَّن الدخول +1$، وعند 10$ يلحق الوقف السعر
على بُعد 5$ من أعلى ربح. لا يرجع الوقف أبداً، ولا نغلق يدوياً، ولا نمس الهدف. مركز بلا وقف يُسجَّل ويُترك.
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
    return {"ticket": ticket, "symbol": p.get("symbol"), "side": side, "entry": entry, "sl": sl,
            "lots": _pick(p, "volume", "lots", "lot")}


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

    def _dollar_target(self, pos, st):
        try:
            lots = float(pos["lots"])
            upp = self.ex.usd_per_price(pos["symbol"], lots)
        except Exception as e:
            if "spec" not in self.warned:
                self.warned.add("spec")
                self.j.event("error", where="adopt_spec", error=str(e))
            return None
        sb = None
        fn = getattr(self.ex, "structure_bars", None)
        if fn is not None:
            try:
                sb = fn(pos["symbol"]) or None
            except Exception as e:
                self.j.event("error", where="adopt_structure", error=str(e))
        smart, state = manage.smart_stop(pos["side"], st["entry"], st["best"], upp, lots, sb)
        st["market"] = state
        base = manage.dollar_stop(pos["side"], st["entry"], st["best"], upp, lots, trail=sb is None)
        if smart is not None and (base is None or manage.is_better_stop(pos["side"], base, smart)):
            return smart
        return base

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
            new = self._dollar_target(pos, st)
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
            self.j.event("trail", ticket=pos["ticket"], old=st["stop"], new=new, manual=True, state=st.get("market"), side=pos["side"])
            st["stop"] = new
        for tk in list(self.state):
            if tk not in seen and tk not in exclude:
                del self.state[tk]       # أُغلق المركز
        st_all = self.j.load_state()
        st_all["adopt"] = self.state
        self.j.save_state(st_all)
