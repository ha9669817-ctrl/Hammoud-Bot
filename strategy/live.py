"""المحلّل الحي: يقرأ الفريمات من الوسيط ويدخل باتجاه السوق فقط (نفس دالة القرار المختبَرة تاريخياً).

لا يدير الوقف بنفسه: صفقاته تُكتشف وتُدار تلقائياً بنفس قاعدة الوقف المتحرك التي تدير أي صفقة يدوية.
أي خطأ هنا يُسجَّل ولا يمسّ بقية الموقع (يُعزل في Runner).
"""
import time

import pandas as pd

from signalbot import manage

from . import ema_cross, signal as sg

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
    def __init__(self, executor, journal, is_enabled, own_on=lambda: True, now=time.time, symbol="", strategy="ema_cross", tf="1h"):
        self.ex, self.j, self.is_enabled, self.own_on, self.now = executor, journal, is_enabled, own_on, now
        self.symbol = (symbol or "").strip()
        self.strategy = strategy if strategy in ("ema_cross", "breakout") else "ema_cross"
        self.tf = tf if tf in ("15m", "1h") else "1h"
        self.state = {"running": False, "error": None, "last_check": None, "last_bar": None,
                      "trend": None, "decision": "لم يبدأ", "entries": 0}
        self.state["strategy"] = self.strategy + (f" {self.tf}" if self.strategy == "ema_cross" else "")
        self.ticket, self.last_bar_ts, self._last_run, self._cool_until = None, None, 0.0, 0.0

    def tick(self):
        t = self.now()
        if t - self._last_run < CHECK_EVERY_S:
            return
        self._last_run = t
        if not self.symbol:
            self.state.update(running=False, decision="لم يُحدَّد رمز (ANALYST_SYMBOL): لا يدخل شيئاً")
            return
        if "XAU" in self.symbol.upper() or "GOLD" in self.symbol.upper():
            self.state.update(running=False, error="ممنوع: المحلّل غير مربوط بالذهب")
            return
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
            rows = self.ex.candle_rows(tf, n, self.symbol)
            if len(rows) < MIN_BARS[tf]:
                raise RuntimeError(f"شموع {tf} غير كافية ({len(rows)}/{MIN_BARS[tf]})")
            out[tf] = _frame(rows)
        return out

    def _step_cross(self, t):
        tfk = {"15m": "M15", "1h": "H1"}[self.tf]
        rows = self.ex.candle_rows(tfk, 320, self.symbol)
        if len(rows) < 215:
            raise RuntimeError(f"شموع {tfk} غير كافية ({len(rows)}/215)")
        df = _frame(rows)
        i = len(df) - 2                                  # آخر شمعة مكتملة
        bar_ts = df.index[i].timestamp()
        self.state.update(last_bar=df.index[i].isoformat())
        if bar_ts == self.last_bar_ts:
            return
        self.last_bar_ts = bar_ts
        if t < self._cool_until:
            self.state["decision"] = "فترة هدوء بعد صفقة"
            return
        dec = ema_cross.decide(df, i)
        if not dec:
            self.state["decision"] = "لا تقاطع مؤكد مع الاتجاه"
            return
        side, sl, tp, ref = dec
        dg = self.ex.price_digits(self.symbol)
        self.ticket = self.ex.place_for(self.symbol, side, round(sl, dg), round(tp, dg))
        self.state.update(entries=self.state["entries"] + 1,
                          decision=f"دخل {side} ~{ref:.5g} وقف {sl:.5g} هدف {tp:.5g}")
        self.j.event("analyst_entry", ticket=self.ticket, symbol=self.symbol, side=side, price=ref, sl=sl, tp=tp,
                     strategy="ema_cross", tf=self.tf)

    def _step(self, t):
        if self.ticket is not None:
            live = {str(k) for k in self.ex.open_tickets_for(self.symbol)}
            if str(self.ticket) in live:
                self.state["decision"] = f"صفقة مفتوحة #{self.ticket}: الوقف يُدار تلقائياً"
                return
            self.ticket, self._cool_until = None, t + COOLDOWN_S
        if self.strategy == "ema_cross":
            return self._step_cross(t)
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
        self.ticket = self.ex.place_for(self.symbol, side, round(sl, 5))
        lots = None
        self.state.update(entries=self.state["entries"] + 1,
                          decision=f"دخل {side} عند ~{price:.2f} وقف {sl:.2f}")
        self.j.event("analyst_entry", ticket=self.ticket, symbol=self.symbol, side=side, price=price, sl=sl,
                     trend=tr, atr=round(float(r["atr"]), 2))
