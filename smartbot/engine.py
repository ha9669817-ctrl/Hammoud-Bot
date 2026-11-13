"""المحرك: يستقبل شمعة مغلقة كل مرة، يدير صفقة واحدة في كل وقت، ويتعلم بعد كل إغلاق.

التعلم هنا قواعد صريحة وقابلة للتدقيق، وليس نموذجاً غامضاً:
1) إيقاف الاستراتيجية إذا كان متوسط آخر 10 صفقات لها سلبياً، ثم إعادة البحث على أحدث البيانات.
2) تجنب مناطق RSI (حسب الاتجاه) التي كانت نسبة نجاحها أقل من 25% في 8 صفقات أو أكثر.
كل قرار يُكتب في lessons.md مع سببه.
"""
from dataclasses import asdict

import numpy as np
import pandas as pd

from .features import build, atom_masks
from .journal import Journal
from .search import build_strategy
from .strategy import COST_R, Spec, signal_mask

RISK_PCT = 1.0
HIST_MAX = 3000
FEAT_TAIL = 1000
RETIRE_MIN_TRADES = 10
AVOID_MIN = 8
AVOID_WIN = 25.0
MIN_BARS_TO_RESEARCH = 600


def rsi_bucket(r: float) -> str:
    if r < 30:
        return "<30"
    if r < 50:
        return "30-50"
    if r < 70:
        return "50-70"
    return ">=70"


def describe(spec: Spec | None) -> str | None:
    if spec is None:
        return None
    side = "شراء" if spec.side == 1 else "بيع"
    return (f"{side} عند تحقق: {' + '.join(spec.atoms)} | وقف {spec.stop_atr} ATR "
            f"| هدف {spec.rr}R | أقصى مدة {spec.max_bars} شمعة")


class SmartBot:
    def __init__(self, journal: Journal | None = None, start_balance: float = 1000.0):
        self.j = journal or Journal()
        st = self.j.load_state()
        self.start_balance = float(st.get("start_balance", start_balance))
        self.balance = float(st.get("balance", start_balance))
        self.spec = Spec(**{**st["spec"], "atoms": tuple(st["spec"]["atoms"])}) if st.get("spec") else None
        self.avoid = set(st.get("avoid", []))
        self.retired = set(st.get("retired", []))
        self.pos = st.get("pos")
        self.last_open = st.get("last_open")
        self.last_close = st.get("last_close")
        self.hist: list = []
        self.learn = True  # يُعطَّل في اختبارات المطابقة فقط

    # ---------- حفظ الحالة ----------
    def _save(self) -> None:
        self.j.save_state({
            "start_balance": self.start_balance,
            "balance": self.balance,
            "spec": asdict(self.spec) if self.spec else None,
            "avoid": sorted(self.avoid),
            "retired": sorted(self.retired),
            "pos": self.pos,
            "last_open": self.last_open,
            "last_close": self.last_close,
        })

    # ---------- بداية التشغيل ----------
    def bootstrap(self, df: pd.DataFrame) -> dict:
        """يبحث عن استراتيجية على تاريخ معروف، ويعتمدها إن تحققت شروط القبول."""
        self.hist = [tuple(r) for r in df[["time", "open", "high", "low", "close"]].itertuples(index=False)]
        return self._research(reason_prefix="بداية التشغيل")

    def _research(self, reason_prefix: str = "إعادة بحث") -> dict:
        n = len(self.hist)
        if n < MIN_BARS_TO_RESEARCH:
            self.spec = None
            self.j.event("research", f"{reason_prefix}: بيانات غير كافية ({n} شمعة)")
            self._save()
            return {"accepted": False}
        df = self._frame(self.hist[-HIST_MAX:])
        res = build_strategy(build(df), 0, len(df))
        spec = res.get("spec")
        if res["accepted"] and spec.key in self.retired:
            res["accepted"] = False
            res["reason"] = "أفضل مرشح سبق إيقافه"
        if res["accepted"]:
            self.spec = spec
            self.j.event("research", f"{reason_prefix}: اعتمدت استراتيجية جديدة",
                         {"spec": describe(spec), "train": res["train"], "validation": res["validation"]["stats"]})
        else:
            self.spec = None
            self.j.event("research", f"{reason_prefix}: لا توجد استراتيجية مقبولة الآن ({res.get('reason', '')})",
                         {"spec": spec.key if spec else None})
        self._save()
        return res

    @staticmethod
    def _frame(rows: list) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
        return df

    # ---------- كل شمعة مغلقة ----------
    def feed(self, ts, o, h, l, c) -> list:
        self.hist.append((ts, float(o), float(h), float(l), float(c)))
        if len(self.hist) > HIST_MAX:
            del self.hist[: len(self.hist) - HIST_MAX]
        k_i = len(self.hist) - 1
        events: list = []
        closed = False

        if self.pos:
            p = self.pos
            if p["pending"]:
                # الدخول عند افتتاح هذه الشمعة (بعد إشارة الشمعة السابقة)
                R = p["stop_atr"] * p["atr"]
                p.update(entry=float(o), entry_ts=ts, R=R, pending=False, e_i=k_i, k=0)
                if p["side"] == 1:
                    p.update(stop=p["entry"] - R, tgt=p["entry"] + p["rr"] * R)
                else:
                    p.update(stop=p["entry"] + R, tgt=p["entry"] - p["rr"] * R)
                self.last_open = {"at": ts, "side": "شراء" if p["side"] == 1 else "بيع",
                                  "price": p["entry"], "stop": p["stop"], "target": p["tgt"]}
                events.append({"kind": "open", **self.last_open})
                self.j.event("open", "فتح صفقة", self.last_open)
            else:
                p["k"] = k_i - p["e_i"]
            closed = self._check_exit(ts, float(h), float(l), float(c), events)

        if self.pos is None and not closed and self.spec is not None and self.balance > 0:
            ok, info = self._signal()
            if ok:
                self.pos = {
                    "side": self.spec.side, "atoms": list(self.spec.atoms), "spec": asdict(self.spec),
                    "stop_atr": self.spec.stop_atr, "rr": self.spec.rr, "max_bars": self.spec.max_bars,
                    "atr": info["atr"], "rsi": info["rsi"], "bucket": info["bucket"],
                    "signal_ts": ts, "risk_usd": round(self.balance * RISK_PCT / 100, 4),
                    "pending": True, "entry": None, "stop": None, "tgt": None, "R": None,
                    "e_i": None, "k": 0,
                }
                events.append({"kind": "signal", "at": ts, "spec": describe(self.spec)})
        self._save()
        return events

    def _signal(self):
        df = self._frame(self.hist[-FEAT_TAIL:])
        f = build(df)
        m = signal_mask(atom_masks(f), self.spec)
        if not m[-1] or np.isnan(f["atr14"][-1]) or f["atr14"][-1] <= 0:
            return False, None
        rsi = float(f["rsi14"][-1])
        bucket = rsi_bucket(rsi)
        side = "شراء" if self.spec.side == 1 else "بيع"
        key = f"{side}|{bucket}"
        if key in self.avoid:
            self.j.event("skip", f"تجاهلت إشارة {side} لأن RSI في منطقة مرفوضة ({bucket})", {"rsi": rsi})
            return False, None
        return True, {"atr": float(f["atr14"][-1]), "rsi": rsi, "bucket": bucket}

    def _check_exit(self, ts, h, l, c, events) -> bool:
        p = self.pos
        R, e, side = p["R"], p["entry"], p["side"]
        reason, res, price = None, None, None
        if side == 1:
            if l <= p["stop"]:
                reason, res, price = "stop", -1.0, p["stop"]
            elif h >= p["tgt"]:
                reason, res, price = "target", p["rr"], p["tgt"]
        else:
            if h >= p["stop"]:
                reason, res, price = "stop", -1.0, p["stop"]
            elif l <= p["tgt"]:
                reason, res, price = "target", p["rr"], p["tgt"]
        if reason is None and p["k"] >= p["max_bars"]:
            reason, price = "time", c
            res = (c - e) / R if side == 1 else (e - c) / R
        if reason is None:
            return False
        net = res - COST_R
        pnl = net * p["risk_usd"]
        self.balance += pnl
        rec = {
            "side": "شراء" if side == 1 else "بيع", "spec": describe(self.spec) or "",
            "spec_key": p["spec"] and Spec(**{**p["spec"], "atoms": tuple(p["atoms"])}).key,
            "signal_ts": p["signal_ts"], "entry_ts": p["entry_ts"], "entry": e,
            "exit_ts": ts, "exit": price, "reason": reason,
            "r_net": round(net, 4), "pnl_usd": round(pnl, 4), "balance_after": round(self.balance, 2),
            "rsi": round(p["rsi"], 2), "bucket": p["bucket"],
        }
        self.j.trade(rec)
        self.last_close = rec
        events.append({"kind": "close", **rec})
        self.pos = None
        self._learn(rec, events)
        return True

    # ---------- التعلم بعد كل صفقة ----------
    def _learn(self, rec: dict, events: list) -> None:
        if not self.learn:
            return
        trades = self.j.read_jsonl(self.j.trades_path, limit=5000)
        if self.spec is not None:
            same = [t for t in trades if t.get("spec_key") == self.spec.key][-RETIRE_MIN_TRADES:]
            if len(same) >= RETIRE_MIN_TRADES and np.mean([t["r_net"] for t in same]) < 0:
                msg = (f"أوقفت الاستراتيجية: متوسط آخر {RETIRE_MIN_TRADES} صفقات سلبي "
                       f"({np.mean([t['r_net'] for t in same]):+.3f}R)")
                self.retired.add(self.spec.key)
                self.j.lesson(msg)
                events.append({"kind": "lesson", "msg": msg})
                self.spec = None
                self._research(reason_prefix="بعد إيقاف الاستراتيجية")
        recent = trades[-60:]
        groups: dict = {}
        for t in recent:
            key = f"{t['side']}|{t['bucket']}"
            groups.setdefault(key, []).append(t["r_net"] > 0)
        for key, wins in groups.items():
            if key in self.avoid or len(wins) < AVOID_MIN:
                continue
            win_rate = 100 * sum(wins) / len(wins)
            if win_rate < AVOID_WIN:
                self.avoid.add(key)
                msg = (f"تجنّب {key}: نسبة النجاح {win_rate:.0f}% من {len(wins)} صفقات "
                       f"في هذه المنطقة")
                self.j.lesson(msg)
                events.append({"kind": "lesson", "msg": msg})

    # ---------- الواجهة ----------
    def status(self) -> dict:
        trades = self.j.read_jsonl(self.j.trades_path, limit=5000)
        wins = sum(1 for t in trades if t["r_net"] > 0)
        losses = len(trades) - wins
        if self.spec is None:
            state = "متوقف: لا توجد استراتيجية معتمدة الآن"
        elif self.pos:
            state = "صفقة مفتوحة"
        else:
            state = "يراقب السوق"
        return {
            "state": state,
            "strategy": describe(self.spec),
            "balance": round(self.balance, 2),
            "start_balance": round(self.start_balance, 2),
            "total_profit": round(self.balance - self.start_balance, 2),
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(100 * wins / len(trades), 1) if trades else None,
            "last_open": self.last_open,
            "last_close": self.last_close,
            "open_position": {k: self.pos.get(k) for k in ("side", "entry", "stop", "tgt")} if self.pos else None,
            "avoid": sorted(self.avoid),
            "lessons": self.j.lessons_tail(15),
            "recent_trades": trades[-10:],
        }
