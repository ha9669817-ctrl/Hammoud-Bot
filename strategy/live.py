"""المحلّل الحي: يقرأ الفريمات من الوسيط ويدخل باتجاه السوق فقط (نفس دالة القرار المختبَرة تاريخياً).

لا يدير الوقف بنفسه: صفقاته تُكتشف وتُدار تلقائياً بنفس قاعدة الوقف المتحرك التي تدير أي صفقة يدوية.
أي خطأ هنا يُسجَّل ولا يمسّ بقية الموقع (يُعزل في Runner).
"""
import time

import pandas as pd

from signalbot import manage

from . import signal as sg

NEED = {"M5": 150, "M15": 150, "H1": 300, "H4": 150}     # شموع مطلوبة لكل فريم
MIN_BARS = {"M5": 60, "M15": 60, "H1": 220, "H4": 60}      # أقل عدد لتكون الـEMA200 وغيرها موثوقة
CHECK_EVERY_S = 20
COOLDOWN_S = 15 * 60
MAX_SL = manage.MAX_SL_DIST


def _frame(rows):
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"])
    df.index = pd.to_datetime(df.pop("t"), unit="s", utc=True)
    return df[~df.index.duplicated()]


class Analyst:
    def __init__(self, executor, journal, is_enabled, own_on=lambda: True, now=time.time):
        self.ex, self.j, self.is_enabled, self.own_on, self.now = executor, journal, is_enabled, own_on, now
        self.state = {"running": False, "error": None, "last_check": None, "last_bar": None,
                      "trend": None, "decision": "لم يبدأ", "entries": 0}
        self.ticket, self.last_bar_ts, self._last_run, self._cool_until = None, None, 0.0, 0.0

    def tick(self):
        t = self.now()
        if t - self._last_run < CHECK_EVERY_S:
            return
        self._last_run = t
        if not (self.is_enabled() and self.own_on()):
            self.state.update(running=False, decision="متوقف")
            return
        try:
            self._step(t)
            self.state.update(running=True, error=None, last_check=time.strftime("%H:%M:%S", time.gmtime(t)))
        except Exception as e:
            self.state.update(running=True, error=f"{type(e).__name__}: {str(e)[:140]}")
            self.j.event("error", where="analyst", error=str(e)[:160])

    def _frames(self):
        out = {}
        for tf, n in NEED.items():
            rows = self.ex.candle_rows(tf, n)
            if len(rows) < MIN_BARS[tf]:
                raise RuntimeError(f"شموع {tf} غير كافية ({len(rows)}/{MIN_BARS[tf]})")
            out[tf] = _frame(rows)
        return out

    def _step(self, t):
        if self.ticket is not None:
            if self.ticket in self.ex.open_tickets() or str(self.ticket) in {str(k) for k in self.ex.open_tickets()}:
                self.state["decision"] = f"صفقة مفتوحة #{self.ticket}: الوقف يُدار تلقائياً"
                return
            self.ticket, self._cool_until = None, t + COOLDOWN_S
        f = self._frames()
        d = sg.prepare(f["M5"], h1=f["H1"], h4=f["H4"], m15=f["M15"])
        i = len(d) - 2                                   # آخر شمعة مكتملة (الأخيرة ما زالت جارية)
        bar_ts = d.index[i].timestamp()
        r = d.iloc[i]
        tr = {"h4": int(r["t_h4"]) if pd.notna(r["t_h4"]) else None, "h1": int(r["t_h1"]) if pd.notna(r["t_h1"]) else None,
              "m15": int(r["t_m15"]) if pd.notna(r["t_m15"]) else None,
              "adx": round(float(r["adx_h1"]), 1) if pd.notna(r["adx_h1"]) else None}
        self.state.update(trend=tr, last_bar=d.index[i].isoformat())
        if bar_ts == self.last_bar_ts:
            return                                       # هذه الشمعة قُيّمت سابقاً
        self.last_bar_ts = bar_ts
        if t < self._cool_until:
            self.state["decision"] = "فترة هدوء بعد صفقة"
            return
        dec = sg.decide(d, i, **sg.LIVE)
        if not dec:
            self.state["decision"] = "لا إشارة: الاتجاه غير متفق أو لا اختراق"
            return
        side, dist = dec
        dist = min(dist, MAX_SL)
        price = float(d["close"].iloc[-1])
        sl = price - dist if side == "buy" else price + dist
        lots, risk_usd, _ = self.ex.size(dist)
        self.ticket = self.ex.place(side, round(sl, 3), None, lots)
        self.state.update(entries=self.state["entries"] + 1,
                          decision=f"دخل {side} عند ~{price:.2f} وقف {sl:.2f}")
        self.j.event("analyst_entry", ticket=self.ticket, side=side, price=price, sl=sl, lots=lots,
                     trend=tr, atr=round(float(r["atr"]), 2))
