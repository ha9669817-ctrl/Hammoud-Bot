"""محرك بوت التوصيات: إشارة ← قرار دخول ← وقف متحرك ← تسجيل ← تعلّم لكل قناة.

التعلّم هنا محدود وصريح: يوقف القناة التي تخسر فعلياً (حماية)، ولا يخلق أفضلية غير موجودة.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import manage
from .parser import parse_signal

RISK_PCT = 1.0            # المخاطرة المستهدفة من الرصيد لكل صفقة
MAX_RISK_PCT = 10.0       # إن كان أصغر لوت يتجاوز هذا السقف تُتجاهل الإشارة
MAX_OPEN = 2              # أقصى صفقات (مفتوحة + معلّقة) معاً
PAUSE_MIN_TRADES = 8      # أقل عدد صفقات مغلقة قبل الحكم على القناة
PAUSE_WINDOW = 15         # نافذة آخر صفقات للحكم


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _append(self, name, obj):
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def event(self, name, **kw):
        self._append("events.jsonl", {"at": _now(), "event": name, **kw})

    def trade(self, rec):
        self._append("trades.jsonl", rec)

    def lesson(self, text):
        with open(self.dir / "lessons.md", "a", encoding="utf-8") as f:
            f.write(f"- {_now()}  {text}\n")

    def read(self, name, last=None):
        p = self.dir / name
        if not p.exists():
            return []
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return rows[-last:] if last else rows

    def lessons_text(self, last=30):
        p = self.dir / "lessons.md"
        return "\n".join(p.read_text(encoding="utf-8").splitlines()[-last:]) if p.exists() else ""

    def load_state(self):
        p = self.dir / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def save_state(self, st):
        (self.dir / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


class SignalBot:
    def __init__(self, journal: Journal, executor, channels, is_enabled=lambda: True):
        self.j, self.ex, self.channels = journal, executor, set(channels)
        self.is_enabled = is_enabled
        st = journal.load_state()
        self.trades = st.get("trades", {})          # ticket(str) -> trade dict (غير مغلقة)
        self.seen = set(st.get("seen", []))
        self.paused = st.get("paused", {})          # channel -> سبب
        self.last_bar_ts = st.get("last_bar_ts")

    # ---------- حفظ ----------
    def _save(self):
        self.j.save_state({"trades": self.trades, "seen": sorted(self.seen)[-2000:],
                           "paused": self.paused, "last_bar_ts": self.last_bar_ts})

    # ---------- إحصاء القنوات ----------
    def channel_stats(self):
        closed = self.j.read("trades.jsonl")
        out = {}
        for ch in sorted(self.channels | {c["channel"] for c in closed}):
            rs = [c["r"] for c in closed if c["channel"] == ch]
            out[ch] = {"trades": len(rs), "sum_r": round(sum(rs), 2),
                       "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                       "wins": sum(1 for r in rs if r > 0), "paused": self.paused.get(ch)}
        return out

    def _review_channel(self, ch):
        rs = [c["r"] for c in self.j.read("trades.jsonl") if c["channel"] == ch][-PAUSE_WINDOW:]
        if len(rs) >= PAUSE_MIN_TRADES and sum(rs) < 0 and ch not in self.paused:
            self.paused[ch] = f"إيقاف تلقائي: مجموع آخر {len(rs)} صفقات = {sum(rs):.2f}R"
            self.j.lesson(f"أوقفت قناة {ch}: {self.paused[ch]}")
            self.j.event("channel_paused", channel=ch, reason=self.paused[ch])

    def resume(self, ch):
        self.paused.pop(ch, None)
        self._save()

    # ---------- استقبال توصية ----------
    def on_message(self, channel, msg_id, text, price):
        """يعيد وصفاً نصياً للقرار (يُسجَّل دائماً)."""
        key = f"{channel}:{msg_id}"
        if key in self.seen:
            return "مكرر"
        self.seen.add(key)
        sig = parse_signal(text)
        if not sig:
            return "ليست توصية"
        def skip(reason):
            self.j.event("skip", channel=channel, msg=msg_id, reason=reason, signal=sig)
            self._save()
            return "تجاهل: " + reason
        if channel not in self.channels:
            return skip("قناة غير مفعّلة")
        if not self.is_enabled():
            return skip("البوت متوقف من اللوحة")
        if channel in self.paused:
            return skip("القناة موقوفة: " + self.paused[channel])
        if len(self.trades) >= MAX_OPEN:
            return skip("بلغ الحد الأقصى للصفقات المتزامنة")
        kind, entry = manage.plan_entry(sig, price)
        if kind == "skip":
            return skip(entry)
        sl_dist = abs(entry - sig["sl"])
        lots, risk_usd, balance = self.ex.size(sl_dist, RISK_PCT)
        if balance and risk_usd / balance * 100 > MAX_RISK_PCT:
            return skip(f"أصغر لوت يخاطر بـ{risk_usd:.2f}$ ({risk_usd / balance * 100:.0f}% من الرصيد)")
        tp = manage.final_target(sig)
        base = {"channel": channel, "msg": msg_id, "side": sig["side"], "kind": kind,
                "entry": entry, "sl0": sig["sl"], "stop": sig["sl"], "tps": sig["tps"], "tp": tp,
                "level": 0, "lots": lots, "risk_usd": risk_usd, "placed_at": _now(),
                "expire_ts": time.time() + manage.PENDING_HOURS * 3600}
        if kind == "pending":
            # انتظار داخلي: عند لمس حافة المنطقة نفتح بسعر السوق (لا نعتمد على أوامر معلّقة لدى الوسيط)
            base.update(ticket=None, state="waiting")
            self.trades["w:" + key] = base
            self.j.event("waiting", channel=channel, msg=msg_id, entry=entry, sl=sig["sl"], tp=tp)
        else:
            try:
                ticket = self.ex.place(sig["side"], sig["sl"], tp, lots)
            except Exception as e:  # الوسيط رفض: نسجّل ولا نعيد المحاولة
                return skip(f"رفض الوسيط: {e}")
            base.update(ticket=ticket, state="open")
            self.trades[str(ticket)] = base
            self.j.event("entry", channel=channel, msg=msg_id, ticket=ticket, entry=entry,
                         sl=sig["sl"], tp=tp, lots=lots, risk_usd=risk_usd)
        self._save()
        return f"{kind} {sig['side']} @ {entry}"

    # ---------- متابعة كل دورة ----------
    def tick(self, bars, live):
        """bars: [(high, low)] منذ الدورة السابقة. live: {ticket: 'open'} من الوسيط."""
        for tk, t in list(self.trades.items()):
            if t["state"] == "waiting":
                self._check_waiting(tk, t, bars)
                continue
            ticket = t["ticket"]
            if (live.get(ticket) or live.get(str(ticket))) is None:
                self._finish_if_closed(tk, t)  # إن لم يظهر سجل الإغلاق ننتظر الدورة التالية
                continue
            for hi, lo in bars:
                new_stop, lvl = manage.next_stop(t["side"], t["entry"], t["sl0"], t["tps"],
                                                 t["stop"], hi, lo, t["level"])
                if manage.is_better_stop(t["side"], t["stop"], new_stop):
                    try:
                        self.ex.modify_sl(ticket, new_stop)
                    except Exception as e:
                        self.j.event("error", where="modify_sl", ticket=ticket, error=str(e))
                        break  # لا نتقدّم في المستوى إن فشل التعديل: نعيد المحاولة بالدورة التالية
                    self.j.event("trail", ticket=ticket, old=t["stop"], new=new_stop, level=lvl)
                    t["stop"] = new_stop
                t["level"] = lvl
        self._save()

    def _check_waiting(self, tk, t, bars):
        if time.time() > t["expire_ts"]:
            self.j.event("expired", channel=t["channel"], entry=t["entry"])
            del self.trades[tk]
            return
        if not any(lo <= t["entry"] <= hi for hi, lo in bars):
            return
        if len([x for x in self.trades.values() if x["state"] == "open"]) >= MAX_OPEN:
            return
        try:
            ticket = self.ex.place(t["side"], t["sl0"], t["tp"], t["lots"])
        except Exception as e:
            self.j.event("error", where="place_waiting", channel=t["channel"], error=str(e))
            del self.trades[tk]
            return
        del self.trades[tk]
        t.update(ticket=ticket, state="open")
        self.trades[str(ticket)] = t
        self.j.event("filled", ticket=ticket, channel=t["channel"], entry=t["entry"])

    def _finish_if_closed(self, tk, t):
        profit = self.ex.closed_profit(t["ticket"], t["placed_at"])
        if profit is None:
            return False  # لم يظهر سجل الإغلاق بعد؛ ننتظر
        r = profit / t["risk_usd"] if t["risk_usd"] else 0.0
        rec = {"ticket": t["ticket"], "channel": t["channel"], "side": t["side"], "entry": t["entry"],
               "sl0": t["sl0"], "last_stop": t["stop"], "level": t["level"], "profit": profit,
               "risk_usd": t["risk_usd"], "r": round(r, 3), "closed_at": _now()}
        self.j.trade(rec)
        outcome = "ربح" if profit > 0 else "خسارة"
        self.j.lesson(f"{t['channel']} {t['side']}: {outcome} {profit:+.2f}$ ({r:+.2f}R) — بلغ المستوى {t['level']}")
        del self.trades[tk]
        self._review_channel(t["channel"])
        return True
