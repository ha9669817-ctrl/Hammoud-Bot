"""توليد استراتيجيات مرشحة من مكتبة الشروط، واختيار الأفضل على بيانات التطوير،
ثم التحقق عليها ببيانات لم تُستخدم في الاختيار. القبول صارم عمداً."""
import math
import numpy as np

from .features import ATOM_NAMES, atom_masks
from .strategy import Spec, signal_mask, simulate, stats

MIN_TRAIN_TRADES = 30
MIN_VAL_TRADES = 30
MIN_VAL_PF = 1.2
TRAIN_FRAC = 0.6


def sample_specs(rng: np.random.Generator, count: int) -> list:
    seen, out = set(), []
    tries = 0
    while len(out) < count and tries < count * 20:
        tries += 1
        k = int(rng.choice([1, 2, 3], p=[0.2, 0.5, 0.3]))
        atoms = tuple(sorted(rng.choice(ATOM_NAMES, size=k, replace=False).tolist()))
        spec = Spec(
            side=int(rng.choice([1, -1])),
            atoms=atoms,
            stop_atr=float(rng.choice([1.0, 1.5, 2.0])),
            rr=float(rng.choice([1.5, 2.0, 3.0])),
            max_bars=48,
        )
        if spec.key not in seen:
            seen.add(spec.key)
            out.append(spec)
    return out


def _slice(f: dict, lo: int, hi: int) -> dict:
    return {k: v[lo:hi] for k, v in f.items()}


def fitness(R: np.ndarray) -> float:
    """متوسط الصفقة مضروباً في جذر عدد الصفقات: يعاقب العينات الصغيرة."""
    if len(R) < MIN_TRAIN_TRADES:
        return -math.inf
    return float(R.mean()) * math.sqrt(len(R))


def run_spec(f: dict, spec: Spec, lo: int, hi: int) -> np.ndarray:
    sub = _slice(f, lo, hi)
    masks = atom_masks(sub)
    return simulate(sub, signal_mask(masks, spec), spec)


def search(f: dict, lo: int, hi: int, count: int = 300, seed: int = 7):
    """يبحث على الفترة [lo, hi) فقط ويرجع أفضل استراتيجية حسب التطوير."""
    rng = np.random.default_rng(seed)
    best, best_fit = None, -math.inf
    for spec in sample_specs(rng, count):
        R = run_spec(f, spec, lo, hi)
        fit = fitness(R)
        if fit > best_fit:
            best, best_fit = spec, fit
    return best


def validate(f: dict, spec: Spec, lo: int, hi: int) -> dict:
    """تحقق خارج العينة. يقبل فقط إذا تحققت كل الشروط معاً."""
    R = run_spec(f, spec, lo, hi)
    s = stats(R)
    mid = lo + (hi - lo) // 2
    h1 = stats(run_spec(f, spec, lo, mid))
    h2 = stats(run_spec(f, spec, mid, hi))
    checks = {
        "عدد_الصفقات_كافٍ": s["n"] >= MIN_VAL_TRADES,
        "متوسط_موجب": s["exp"] > 0,
        "عامل_ربح_كافٍ": s["pf"] is not None and s["pf"] >= MIN_VAL_PF,
        "النصف_الأول_موجب": h1["n"] > 0 and h1["exp"] > 0,
        "النصف_الثاني_موجب": h2["n"] > 0 and h2["exp"] > 0,
    }
    return {"stats": s, "halves": [h1, h2], "checks": checks, "accepted": all(checks.values())}


def build_strategy(f: dict, lo: int, hi: int, count: int = 300, seed: int = 7) -> dict:
    """يقسم [lo, hi) إلى تطوير وتحقق، يبحث على التطوير، ويتحقق على الباقي."""
    cut = lo + int((hi - lo) * TRAIN_FRAC)
    spec = search(f, lo, cut, count=count, seed=seed)
    if spec is None:
        return {"accepted": False, "reason": "لا توجد استراتيجية بعدد صفقات كافٍ على بيانات التطوير", "spec": None}
    v = validate(f, spec, cut, hi)
    result = {"spec": spec, "train": stats(run_spec(f, spec, lo, cut)), "validation": v, "accepted": v["accepted"]}
    if not v["accepted"]:
        failed = [k for k, ok in v["checks"].items() if not ok]
        result["reason"] = "فشل التحقق: " + "، ".join(failed)
    return result
