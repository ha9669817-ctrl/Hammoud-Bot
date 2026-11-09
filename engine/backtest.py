"""إعادة تشغيل تاريخية. قاعدة الحذر: إذا وصل الهدف والوقف في الشمعة نفسها يُحسب وقف الخسارة أولاً.
الصفقات لا تتداخل: إشارة جديدة تُتجاهل حتى تُغلق الصفقة الحالية."""
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass
class Trade:
    side: str
    entry_i: int
    exit_i: int
    entry: float
    stop: float
    target: float
    r: float  # الربح/الخسارة بوحدة المخاطرة R بعد التكلفة


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def replay(df: pd.DataFrame, buy, sell, atr_mult: float = 1.5, rr: float = 2.0,
           cost_r: float = 0.05) -> list:
    a = atr(df).to_numpy()
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    trades, i, n = [], 0, len(df)
    while i < n - 1:
        side = "buy" if buy[i] else ("sell" if sell[i] else None)
        if side is None or np.isnan(a[i]) or (buy[i] and sell[i]):
            i += 1
            continue
        entry = c[i]
        dist = a[i] * atr_mult
        if side == "buy":
            stop, target = entry - dist, entry + dist * rr
        else:
            stop, target = entry + dist, entry - dist * rr
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue
        exit_i, r = n - 1, None
        for j in range(i + 1, n):
            hit_stop = l[j] <= stop if side == "buy" else h[j] >= stop
            hit_target = h[j] >= target if side == "buy" else l[j] <= target
            if hit_stop:  # الحذر: الوقف أولاً عند التعارض داخل الشمعة
                exit_i, r = j, -1.0
                break
            if hit_target:
                exit_i, r = j, rr
                break
        if r is None:  # لم تُغلق الصفقة حتى نهاية البيانات: تُغلق عند آخر إغلاق
            last = c[-1]
            r = ((last - entry) if side == "buy" else (entry - last)) / risk
        trades.append(Trade(side, i, exit_i, float(entry), float(stop), float(target),
                            round(r - cost_r, 4)))
        i = exit_i + 1
    return trades


def metrics(trades: list) -> dict:
    rs = np.array([t.r for t in trades], dtype=float)
    if len(rs) == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy_r": 0.0, "max_drawdown_r": 0.0, "total_r": 0.0}
    wins, losses = rs[rs > 0].sum(), -rs[rs <= 0].sum()
    equity = np.cumsum(rs)
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    dd = float((peak - equity).max()) if len(equity) else 0.0
    return {
        "trades": int(len(rs)),
        "win_rate": round(float((rs > 0).mean()), 4),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else float("inf"),
        "expectancy_r": round(float(rs.mean()), 4),
        "max_drawdown_r": round(dd, 3),
        "total_r": round(float(rs.sum()), 3),
    }


def trades_as_dicts(trades: list) -> list:
    return [asdict(t) for t in trades]
