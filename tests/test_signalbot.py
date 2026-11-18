import tempfile
import time

from signalbot import manage
from signalbot.engine import Journal, SignalBot
from signalbot.parser import parse_signal

STRUCT = """📈 XAUUSD — BUY 🟢
🎯 Entry Zone: 4038 - 4048
🟢 TP1: 4053
🟢 TP2: 4058
🟢 TP3: 4063
🔴 Stop Loss (SL): 4033"""
LIQ = "SELL LIMIT XAUUSD ENTRY 4139 - 4141 SL. 4143 Tp1. 4137 Tp2. 4135 Tp3. 4133"
MASTER = "Gold buy now 4420-4422\nSL: 4408\nTP: 4425\nTP2: 4430\nTP3: 4440"
MASTER_MKT = "Gold sell now\nSL: 4450\nTP1: 4430\nTP2: 4425"


def test_parse_formats():
    s = parse_signal(STRUCT)
    assert s["side"] == "buy" and (s["lo"], s["hi"], s["sl"]) == (4038, 4048, 4033) and len(s["tps"]) == 3
    s = parse_signal(LIQ)
    assert s["side"] == "sell" and s["sl"] == 4143 and s["tps"][0] == 4137
    s = parse_signal(MASTER)
    assert s["side"] == "buy" and (s["lo"], s["hi"]) == (4420, 4422)
    s = parse_signal(MASTER_MKT)
    assert s["lo"] is None and s["side"] == "sell"


def test_parse_rejects_noise_and_bad_geometry():
    assert parse_signal("Tp3✅ +300 pips") is None
    assert parse_signal("BTC buy 60000 SL 59000 TP1 61000") is None
    # وقف على الجهة الخطأ
    assert parse_signal("XAUUSD BUY ENTRY 4000 - 4002 SL 4010 TP1 4020") is None


def test_plan_entry():
    sig = parse_signal(STRUCT)                       # شراء 4038-4048، وقف 4033
    assert manage.plan_entry(sig, 4040)[0] == "market"       # داخل المنطقة
    assert manage.plan_entry(sig, 4050) == ("pending", 4048)  # فوق المنطقة: ينتظر النزول إليها
    assert manage.plan_entry(sig, 4100) == ("pending", 4048)  # حتى لو بعيد 50$ ينتظر
    assert manage.plan_entry(sig, 4036)[0] == "market"       # تحت المنطقة = سعر أفضل: دخول فوري
    assert manage.plan_entry(sig, 4033.5)[0] == "skip"       # لا مجال قبل الوقف
    sell = parse_signal("XAUUSD SELL\nEntry: 3908 - 3918\nSL: 3923\nTP1: 3903")
    assert manage.plan_entry(sell, 3905) == ("pending", 3908)  # تحت المنطقة: ينتظر الصعود
    assert manage.plan_entry(sell, 3920)[0] == "market"        # فوق المنطقة = أفضل للبيع


def test_trailing_never_moves_back_and_locks_profit():
    tps = [4053, 4058, 4063]
    stop, lvl = manage.next_stop("buy", 4040, 4033, tps, 4033, 4054, 4041, 0)
    assert lvl == 1 and abs(stop - (4040 + 0.7)) < 1e-9
    stop2, lvl2 = manage.next_stop("buy", 4040, 4033, tps, stop, 4050, 4030, lvl)
    assert (stop2, lvl2) == (stop, lvl)
    stop3, lvl3 = manage.next_stop("buy", 4040, 4033, tps, stop, 4059, 4045, lvl)
    assert lvl3 == 2 and stop3 == 4053
    # بيع
    s, l = manage.next_stop("sell", 4140, 4143, [4137, 4135, 4133], 4143, 4139, 4136, 0)
    assert l == 1 and abs(s - (4140 - 0.3)) < 1e-9


class FakeEx:
    def __init__(self, balance=1000.0, risk_per_lot=100.0):
        self.balance, self.rpl = balance, risk_per_lot
        self.live, self.modified, self.profit, self.next = {}, [], None, 100
        self.fail_modify = False

    def size(self, sl_dist, _unused=None):
        lots = 0.01
        return lots, lots * sl_dist * self.rpl, self.balance

    def place(self, side, sl, tp, lots):
        self.next += 1
        self.live[self.next] = "open"
        return self.next

    def modify_sl(self, ticket, sl):
        if self.fail_modify:
            raise RuntimeError("boom")
        self.modified.append((ticket, sl))

    def closed_profit(self, ticket, since):
        return self.profit


def make(ex=None, channels=("A",), enabled=True):
    d = tempfile.mkdtemp()
    ex = ex or FakeEx()
    return SignalBot(Journal(d), ex, channels, is_enabled=lambda: enabled), ex, d


def test_entry_trail_and_close_journal():
    bot, ex, _ = make()
    assert bot.on_message("A", 1, STRUCT, 4040).startswith("market")
    t = list(ex.live)[0]
    bot.tick([(4054, 4041)], dict(ex.live))
    assert ex.modified and ex.modified[-1][0] == t
    ex.live.clear(); ex.profit = 12.5
    bot.tick([], dict(ex.live))
    rows = bot.j.read("trades.jsonl")
    assert len(rows) == 1 and rows[0]["profit"] == 12.5 and not bot.trades
    assert "ربح" in bot.j.lessons_text()


def test_dedup_disabled_and_unknown_channel():
    bot, ex, _ = make()
    assert bot.on_message("A", 1, STRUCT, 4040).startswith("market")
    assert bot.on_message("A", 1, STRUCT, 4040) == "مكرر"
    assert "غير مفعّلة" in bot.on_message("Z", 2, STRUCT, 4040)
    off, _, _ = make(enabled=False)
    assert "متوقف" in off.on_message("A", 3, STRUCT, 4040)


def test_far_price_waits_and_tiny_account_still_trades():
    bot, ex, _ = make()
    assert bot.on_message("A", 1, STRUCT, 4100).startswith("pending")   # بعيد: أمر معلّق لا تجاهل
    small, ex2, _ = make(FakeEx(balance=20.0, risk_per_lot=100.0))
    r = small.on_message("A", 1, STRUCT, 4040)
    assert r.startswith("market") and len(ex2.live) == 1                # حساب 20$ يدخل بأصغر لوت


def test_second_signal_is_queued_then_taken_when_free():
    bot, ex, _ = make()
    bot.on_message("A", 1, STRUCT, 4040)
    assert "الطابور" in bot.on_message("A", 2, MASTER, 4421)
    assert len(ex.live) == 1
    bot.tick([], dict(ex.live), 4421)          # ما زالت الأولى مفتوحة: الطابور ينتظر
    assert len(ex.live) == 1
    ex.live.clear(); ex.profit = 3.0
    bot.tick([], {}, 4421)                     # أُغلقت الأولى ← تُفتح الثانية
    assert len(ex.live) == 1 and bot.j.read("trades.jsonl")


def test_queued_signal_becomes_waiting_if_price_not_at_zone():
    bot, ex, _ = make()
    bot.on_message("A", 1, STRUCT, 4040)
    bot.on_message("A", 2, MASTER, 4430)       # وقت وصولها السعر فوق المنطقة ولم يلمسها
    ex.live.clear(); ex.profit = 3.0
    bot.tick([], {}, 4600)                     # السعر فوق منطقة الشراء: تنتظر الرجوع إليها
    assert not ex.live
    assert any(t["state"] == "waiting" for t in bot.trades.values())


def test_modify_failure_is_retried_next_tick():
    bot, ex, _ = make()
    bot.on_message("A", 1, STRUCT, 4040)
    ex.fail_modify = True
    bot.tick([(4054, 4041)], dict(ex.live))
    t = next(iter(bot.trades.values()))
    assert t["level"] == 0 and not ex.modified
    ex.fail_modify = False
    bot.tick([(4054, 4041)], dict(ex.live))
    assert ex.modified and t["level"] == 1


def test_losing_channel_gets_paused_and_state_survives_restart():
    bot, ex, d = make()
    for i in range(8):
        bot.on_message("A", i, STRUCT.replace("4033", "4034"), 4040)
        ex.live.clear(); ex.profit = -5.0
        bot.tick([], {})
    assert "A" in bot.paused
    assert "موقوفة" in bot.on_message("A", 99, STRUCT, 4040)
    again = SignalBot(Journal(d), FakeEx(), ["A"])
    assert "A" in again.paused
    again.resume("A")
    assert "A" not in again.paused


def test_waiting_zone_fills_when_price_touches_edge():
    bot, ex, _ = make()
    assert bot.on_message("A", 1, STRUCT, 4050).startswith("pending")
    assert not ex.live
    bot.tick([(4060, 4051)], {})            # لم يلمس 4048
    assert not ex.live
    bot.tick([(4052, 4047)], {})            # لمس الحافة
    assert len(ex.live) == 1 and next(iter(bot.trades.values()))["state"] == "open"


def test_waiting_expires():
    bot, ex, _ = make()
    bot.on_message("A", 1, STRUCT, 4050)
    for t in bot.trades.values():
        t["expire_ts"] = time.time() - 1
    bot.tick([(4052, 4047)], {})
    assert not bot.trades and not ex.live


FF_BUY = "BUY  GOLD      👉  3870 / 3866\n\nTP1.                 👉 3885\n\nTP2.                 👉 3900\n\nTP3.                 👉 3940\n\nTP4.                 👉 3980\n\nTP5.                 👉 4085\n\nSL.                  👉  3855\n\nافتح حساب تحت وكالتي بشركة Vantage\nhttps://vigco.co/la-com-inv/F8nHlAnk\nhttps://one.exnessonelink.com/a/tt6qgt6q\n@MohamedForex3"
FF_SELL = "SELL  GOLD      👉  3868/ 3872\n\nTP1.   👉 3855\n\nTP2.   👉 3840\n\nSL.   👉  3880"
MB_IMG = "Gold sell now 4112-4110\n\nSL: 4127\n\nTP1: 4108\nTP2: 4104\nTP3: 4100\nTP4: 4096\nTP5: 4092\nملاحظة: التزم باللوت المناسب وإدارة رأس المال"
MB_MD = "**Gold buy now 4335-**4336\n\n**Second buy limit 4325-**4326\n\n**SL: **4316\n**TP:** 4346"
LIQ_2 = "SELL LIMIT XAUUSD\nENTRY 4193.7 - 4195.7\nSL.  4198\nTp1. 4191.7\nTp2. 4189.5\nTp3. 4186.5"


def test_real_channel_samples():
    s = parse_signal(FF_BUY, "Free Forex Signals")
    assert (s["side"], s["lo"], s["hi"], s["sl"], s["tps"][0]) == ("buy", 3866, 3870, 3855, 3885)
    s = parse_signal(FF_SELL, "Free Forex Signals")
    assert (s["side"], s["lo"], s["hi"], s["sl"]) == ("sell", 3868, 3872, 3880)
    s = parse_signal(MB_IMG, "Master blue")
    assert (s["side"], s["lo"], s["hi"], s["sl"], len(s["tps"])) == ("sell", 4110, 4112, 4127, 5)
    s = parse_signal(MB_MD, "Master blue")
    assert (s["side"], s["lo"], s["hi"], s["sl"], s["tps"]) == ("buy", 4335, 4336, 4316, [4346])
    s = parse_signal(LIQ_2, "Liquidity Trader")
    assert (s["side"], s["lo"], s["hi"], s["sl"]) == ("sell", 4193.7, 4195.7, 4198)


def test_other_pairs_and_summaries_ignored():
    assert parse_signal("BUY EURUSD 👉 1.0850\nTP1 👉 1.0900\nSL 👉 1.0800", "Free Forex Signals") is None
    assert parse_signal("صفقات يوم الاثنين\nGold buy - SL 40pip❌\nGold sell - tp 88pip✅", "Liquidity Trader") is None


def test_dollar_stop_levels_and_lot_scaling():
    f = manage.dollar_stop
    assert f("buy", 100, 104, 1.0, 0.01) is None            # ربح 4$ أقل من حد التأمين
    assert f("buy", 100, 106, 1.0, 0.01) == 101             # 6$ ← الدخول + 1$
    assert f("buy", 100, 112, 1.0, 0.01) == 107             # 12$ ← وقف عند ربح 7$ (5$ خلف السعر)
    assert f("sell", 100, 85, 1.0, 0.01) == 90              # بيع: ربح 15$ ← وقف عند ربح 10$
    assert f("buy", 100, 104, 2.0, 0.02) is None            # 0.02 لوت: ربح 8$ والعتبة صارت 10$
    assert f("buy", 100, 105, 2.0, 0.02) == 101             # ربح 10$ ← تأمين الدخول + 2$


def test_signal_without_targets_is_parsed_and_uses_dollar_trail():
    sig = parse_signal("Gold buy now 4420-4422\nSL: 4408", "Master blue")
    assert sig and sig["tps"] == [] and manage.final_target(sig) is None
    bot, ex, _ = make()
    assert bot.on_message("Master blue" if False else "A", 1, "Gold buy now 4420-4422\nSL: 4408", 4421).startswith("market")
    t = next(iter(bot.trades.values()))
    assert t["tp"] is None and t["usd_pp"] > 0
    bot.tick([(4430, 4421)], dict(ex.live), 4429)           # ربح ~9$ ← تأمين الدخول+1$ (ليس 10$ بعد)
    assert ex.modified and abs(ex.modified[-1][1] - (4421 + 1.0 * 1 / 1)) < 5
    bot.tick([(4436, 4428)], dict(ex.live), 4435)           # ربح 15$ ← الوقف خلف أعلى سعر بـ5$
    assert abs(ex.modified[-1][1] - (4436 - 5)) < 1.0


def test_trail_not_set_on_wrong_side_of_price():
    bot, ex, _ = make()
    bot.on_message("A", 1, "Gold buy now 4420-4422\nSL: 4408", 4421)
    bot.tick([(4436, 4420)], dict(ex.live), 4425)           # السعر رجع تحت الوقف المطلوب
    assert not ex.modified


TS_IMG = "📉 XAUUSD — SELL 🔴\n\n🎯 Entry Zone: 3918 - 3908\n\n🟢 TP1: 3903\n🟢 TP2: 3900\n🟢 TP3: 3897\n🟢 TP4: 3894\n🟢 TP5: 3890\n\n🔴 Stop Loss (SL): 3923 🔴"


def test_trade_sniper_sample_from_screenshot():
    s = parse_signal(TS_IMG, "Trade Sniper")
    assert (s["side"], s["lo"], s["hi"], s["sl"], len(s["tps"])) == ("sell", 3908, 3918, 3923, 5)


def test_same_price_distances_for_every_lot_size():
    f = manage.dollar_stop
    for lots in (0.01, 0.02, 0.1, 0.5, 1.0):
        upp = 100.0 * lots                                   # الذهب: 1.0 حركة = 100$ لكل لوت
        assert f("buy", 4000, 4004.9, upp, lots) is None     # قبل 5 نقاط: لا شيء
        assert abs(f("buy", 4000, 4005, upp, lots) - 4001) < 1e-9
        assert abs(f("buy", 4000, 4012, upp, lots) - 4007) < 1e-9
        assert abs(f("sell", 4000, 3985, upp, lots) - 3990) < 1e-9


def test_queued_signal_chases_if_price_touched_zone_then_left():
    bot, ex, _ = make()
    bot.on_message("A", 1, STRUCT, 4040)
    bot.on_message("A", 2, MASTER, 4430)                    # شراء 4420-4422 والسعر فوقها: لم يصل بعد
    bot.tick([(4432, 4419)], dict(ex.live), 4425)           # أثناء الانتظار لمس المنطقة
    ex.live.clear(); ex.profit = 3.0
    bot.tick([], {}, 4435)                                  # أُغلقت الأولى والسعر ابتعد لأعلى
    assert len(ex.live) == 1                                # يلحقه فوراً بسعر السوق


def test_queued_signal_waits_if_price_never_touched_zone():
    bot, ex, _ = make()
    bot.on_message("A", 1, STRUCT, 4040)
    bot.on_message("A", 2, MASTER, 4430)
    bot.tick([(4440, 4428)], dict(ex.live), 4435)           # لم تلمس المنطقة
    ex.live.clear(); ex.profit = 3.0
    bot.tick([], {}, 4435)
    assert not ex.live and any(t["state"] == "waiting" for t in bot.trades.values())


from signalbot import structure


def _uptrend(n=40, base=4000.0):
    """صعود منتظم بقيعان واضحة: كل شمعة أعلى من سابقتها بقليل."""
    bars = []
    for i in range(n):
        lo = base + i * 1.0 + (0.0 if i % 4 else -0.8)
        bars.append((lo + 2.0, lo, lo + 1.5))
    return bars


def test_structure_pullback_keeps_wide_stop_and_reversal_tightens():
    bars = _uptrend()
    best = max(h for h, _, _ in bars)
    a = structure.analyze("buy", 4000, best, bars)
    assert a["state"] in ("trend", "pullback") and a["stop"] <= best - structure.ROOM_MIN * a["atr"] + 1e-9
    # انعكاس: آخر شمعة تكسر القاع المؤكد بقوة
    swing = a["swing"]
    broken = bars[:-1] + [(swing + 1, swing - 3.0, swing - 2.5)]
    r = structure.analyze("buy", 4000, best, broken)
    assert r["state"] == "reversal" and r["stop"] > a["stop"]          # أضيق من حالة التصحيح


def test_structure_sell_mirrors_buy_and_short_history_is_none():
    bars = [(h, l, c) for h, l, c in _uptrend()]
    mirrored = [(-l, -h, -c) for h, l, c in bars]
    b = structure.analyze("buy", 4000, max(h for h, _, _ in bars), bars)
    s = structure.analyze("sell", -4000, min(l for _, l, _ in mirrored), mirrored)
    assert s["state"] == b["state"] and abs(s["stop"] + b["stop"]) < 1e-6
    assert structure.analyze("buy", 4000, 4010, bars[:10]) is None


def test_smart_stop_only_after_ten_dollars_and_respects_floor():
    bars = _uptrend()
    best = max(h for h, _, _ in bars)
    assert manage.smart_stop("buy", best - 6, best, 1.0, 0.01, bars) == (None, None)   # ربح 6$ فقط
    stop, state = manage.smart_stop("buy", best - 20, best, 1.0, 0.01, bars)
    assert state and stop >= (best - 20) + 1.0 and stop < best


def test_last_target_is_final_and_ladder_covers_all_targets():
    sig = {"tps": [4053, 4058, 4063, 4068, 4073]}
    assert manage.final_target(sig) == 4073
    stop, lvl = manage.next_stop("buy", 4040, 4033, sig["tps"], 4033, 4069, 4041, 0)
    assert lvl == 4 and stop == 4063                                  # بلغ TP4 ← الوقف عند TP3
