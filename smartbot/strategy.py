"""تعريف الاستراتيجية، توليد الإشارات، والمحاكاة مع التكلفة.

قواعد التنفيذ (نفسها في الاختبار والتشغيل):
- الإشارة تُحسب عند إغلاق الشمعة i.
- الدخول عند افتتاح الشمعة i+1.
- الوقف = الدخول ∓ stop_atr × ATR(i)، والهدف = الدخول ± rr × مسافة الوقف.
- إذا لمست الشمعة الوقف والهدف معاً نفترض الوقف أولاً (تحفظ).
- خروج إجباري بعد max_bars شمعة بسعر الإغلاق.
- تكلفة ثابتة تُطرح من كل صفقة (بوحدة R).
"""
from dataclasses import dataclass
import numpy as np

from .features import atom_masks

COST_R = 0.10


@dataclass(frozen=True)
class Spec:
    side: int                 # 1 شراء، -1 بيع
    atoms: tuple              # أسماء الشروط (يجب أن تتحقق كلها معاً)
    stop_atr: float
    rr: float
    max_bars: int = 48

    @property
    def key(self) -> str:
        s = "شراء" if self.side == 1 else "بيع"
        return f"{s}|{'+'.join(self.atoms)}|SL{self.stop_atr}ATR|RR{self.rr}|T{self.max_bars}"


def signal_mask(masks: dict, spec: Spec) -> np.ndarray:
    idx = 0 if spec.side == 1 else 1
    n = len(next(iter(masks.values()))[0])
    m = np.ones(n, dtype=bool)
    for name in spec.atoms:
        m &= np.asarray(masks[name][idx], dtype=bool)
    return m


def simulate(f: dict, sig: np.ndarray, spec: Spec, cost_r: float = COST_R) -> np.ndarray:
    """يرجع مصفوفة نتائج الصفقات بوحدة R بعد التكلفة، وقائمة أزمنة الإغلاق (فهارس)."""
    o, h, l, c, atr = f["open"], f["high"], f["low"], f["close"], f["atr14"]
    n = len(c)
    out = []
    i = 0
    while i < n - 1:
        if not sig[i] or np.isnan(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        e = i + 1
        entry = o[e]
        R = spec.stop_atr * atr[i]
        if spec.side == 1:
            stop, tgt = entry - R, entry + spec.rr * R
        else:
            stop, tgt = entry + R, entry - spec.rr * R
        end = min(n - 1, e + spec.max_bars)
        res, j = None, end
        for j in range(e, end + 1):
            if spec.side == 1:
                if l[j] <= stop:
                    res = -1.0
                    break
                if h[j] >= tgt:
                    res = spec.rr
                    break
            else:
                if h[j] >= stop:
                    res = -1.0
                    break
                if l[j] <= tgt:
                    res = spec.rr
                    break
        if res is None:
            res = ((c[end] - entry) if spec.side == 1 else (entry - c[end])) / R
            j = end
        out.append(res - cost_r)
        i = j + 1
    return np.array(out, dtype=float)


def stats(R: np.ndarray) -> dict:
    n = int(len(R))
    if n == 0:
        return {"n": 0, "win": None, "exp": 0.0, "total": 0.0, "pf": None}
    w = R[R > 0].sum()
    lo = -R[R <= 0].sum()
    return {
        "n": n,
        "win": round(100 * float((R > 0).mean()), 1),
        "exp": round(float(R.mean()), 4),
        "total": round(float(R.sum()), 2),
        "pf": round(float(w / lo), 2) if lo > 0 else None,
    }
