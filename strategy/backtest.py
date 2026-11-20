"""محاكي صفقات M5 (صفقة واحدة في كل مرة، لوت 0.01 = 1$ لكل 1.0 حركة). حذِر: في الشمعة الواحدة يُفترض أن الوقف يُضرب قبل أي ربح."""
import numpy as np

from signalbot import manage

from . import signal as sg

COST = 0.20     # سبريد/عمولة بالدولار لكل صفقة (0.01 لوت)


def simulate(d, start=0, end=None, tp_r=None, cooldown=3, **kw):
    end = end or len(d)
    hi, lo, op, cl = (d[c].to_numpy() for c in ("high", "low", "open", "close"))
    out, i = [], max(start, 60)
    while i < end - 2:
        dec = sg.decide(d, i, **kw)
        if not dec:
            i += 1
            continue
        side, dist = dec
        entry = op[i + 1]
        sl = entry - dist if side == "buy" else entry + dist
        tp = None if tp_r is None else (entry + tp_r * dist if side == "buy" else entry - tp_r * dist)
        stop, best, j, pnl = sl, entry, i + 1, None
        while j < end:
            h, l = hi[j], lo[j]
            if side == "buy":
                if l <= stop: pnl = stop - entry; break
                if tp is not None and h >= tp: pnl = tp - entry; break
                best = max(best, h)
            else:
                if h >= stop: pnl = entry - stop; break
                if tp is not None and l <= tp: pnl = entry - tp; break
                best = min(best, l)
            new = manage.dollar_stop(side, entry, best, 1.0, 0.01, trail=True)
            if new is not None and manage.is_better_stop(side, stop, new):
                stop = new
            j += 1
        if pnl is None:
            break
        out.append({"i": i, "side": side, "pnl": pnl - COST, "bars": j - i})
        i = j + cooldown
    return out


def stats(trades):
    if not trades:
        return {"n": 0}
    p = np.array([t["pnl"] for t in trades])
    gw, gl = p[p > 0].sum(), -p[p < 0].sum()
    return {"n": len(p), "net": round(p.sum(), 1), "avg": round(p.mean(), 2), "win%": round((p > 0).mean() * 100, 1),
            "pf": round(gw / gl, 2) if gl else None, "maxdd": round((np.maximum.accumulate(p.cumsum()) - p.cumsum()).max(), 1)}
