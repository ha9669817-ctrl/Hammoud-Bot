import tempfile

from signalbot.engine import Journal, SignalBot
from signalbot.runner import Runner

STRUCT = "XAUUSD BUY\nEntry: 4038 - 4048\nSL: 4033\nTP1: 4053\nTP2: 4058\nTP3: 4063"


class Ex:
    def __init__(self):
        self.live, self.mods, self.n = {}, [], 0
        self.bars = ([], 100.0, 4040.0)

    def recent_bars(self, since): return self.bars
    def open_tickets(self): return dict(self.live)
    def size(self, d, p=None): return 0.01, 0.07, 1000.0
    def place(self, side, sl, tp, lots):
        self.n += 1; self.live[self.n] = "open"; return self.n
    def modify_sl(self, t, sl): self.mods.append((t, sl))
    def closed_profit(self, t, s): return None


def test_first_step_ignores_old_bars_then_trails():
    ex = Ex()
    bot = SignalBot(Journal(tempfile.mkdtemp()), ex, ["A"], is_enabled=lambda: True)
    r = Runner(bot, ex)
    ex.bars = ([(4060.0, 4030.0)], 100.0, 4040.0)   # شمعة قديمة لا تُحتسب
    r.push("A", 1, STRUCT)
    r.step()
    assert ex.live and not ex.mods                  # دخل، ولم يحرّك الوقف بشموع قديمة
    ex.bars = ([(4054.0, 4041.0)], 160.0, 4050.0)
    r.step()
    assert ex.mods


def test_message_without_price_is_skipped():
    ex = Ex(); ex.bars = ([], None, None)
    bot = SignalBot(Journal(tempfile.mkdtemp()), ex, ["A"], is_enabled=lambda: True)
    r = Runner(bot, ex)
    r.push("A", 1, STRUCT); r.step()
    assert not ex.live


from signalbot.adopt import ManualManager, normalize, target_stop


def test_target_stop_ladder():
    assert target_stop("buy", 100, 90, 105) is None
    assert target_stop("buy", 100, 90, 110) == 101
    assert target_stop("buy", 100, 90, 120) == 110
    assert target_stop("sell", 100, 110, 90) == 99
    assert target_stop("sell", 100, 110, 80) == 90


class MEx:
    def __init__(self):
        self.pos = [{"ticket": 7, "symbol": "XAUUSDm", "side": "BUY", "openPrice": 100.0, "stopLoss": 90.0, "volume": 0.01}]
        self.mods, self.bars = [], ([], 1, 100.0)

    def positions_raw(self): return self.pos
    def usd_per_price(self, sym, lots): return 100.0 * lots   # 0.01 لوت = 1$ لكل 1.0 حركة
    def bars_for(self, sym, since): return self.bars
    def modify_sl(self, t, sl): self.mods.append((t, sl))


def test_manual_positions_adopted_and_trailed_many():
    ex = MEx()
    ex.pos.append({"ticket": 8, "symbol": "XAUUSDm", "side": "SELL", "openPrice": 100.0, "stopLoss": 110.0, "volume": 0.01})
    ex.pos.append({"ticket": 9, "symbol": "XAUUSDm", "side": "BUY", "openPrice": 100.0, "volume": 0.01})
    j = Journal(tempfile.mkdtemp())
    m = ManualManager(ex, j)
    m.tick(set(), 1)
    assert not ex.mods
    ex.bars = ([(111.0, 99.0)], 2, 108.0)
    m.tick(set(), 2)
    assert (7, 106.0) in ex.mods and len(ex.mods) == 1   # ربح 11$ ← وقف عند ربح 6$
    ex.pos[0]["stopLoss"] = 106.0
    ex.bars = ([(104.0, 88.0)], 3, 92.0)
    m.tick(set(), 3)
    assert (8, 93.0) in ex.mods                      # ربح 12$ ← وقف عند ربح 7$
    assert not any(t == 9 for t, _ in ex.mods)
    assert any(e.get("event") == "adopt_no_sl" for e in j.read("events.jsonl"))


def test_bot_own_tickets_are_not_touched_by_manual_manager():
    ex = MEx(); ex.bars = ([(130.0, 99.0)], 2, 125.0)
    m = ManualManager(ex, Journal(tempfile.mkdtemp()))
    m.tick({"7"}, 1)
    assert not ex.mods


def test_normalize_unreadable_returns_none():
    assert normalize({"foo": 1}) is None


def test_trail_continues_by_dollars_when_structure_bars_are_too_few():
    """الخلل الحقيقي: شموع هيكل موجودة لكن <25 فكان الوقف يتحرك مرة واحدة فقط (دخول+1$) ثم يتجمّد."""
    ex = MEx()
    ex.structure_bars = lambda sym=None: [(101.0, 99.0, 100.0)] * 10     # قليلة: لا تكفي للتحليل
    j = Journal(tempfile.mkdtemp())
    m = ManualManager(ex, j)
    m.tick(set(), 1)
    ex.bars = ([(106.0, 99.0)], 2, 105.0)            # ربح 6$: تأمين الدخول +1$
    m.tick(set(), 2)
    assert ex.mods[-1] == (7, 101.0)
    ex.pos[0]["stopLoss"] = 101.0
    ex.bars = ([(128.0, 104.0)], 3, 127.0)           # ربح 28$: يجب أن يلحق الوقف 5$ خلف القمة
    m.tick(set(), 3)
    assert ex.mods[-1] == (7, 123.0)
    ex.pos[0]["stopLoss"] = 123.0
    ex.bars = ([(127.0, 110.0)], 4, 111.0)           # نزل السعر: لا يتراجع الوقف
    m.tick(set(), 4)
    assert ex.mods[-1] == (7, 123.0)


def _flat_bars(n=60, price=100.0, rng=3.0):
    """شموع هادئة ATR≈3 تكفي للتحليل الهيكلي."""
    return [(price + rng / 2, price - rng / 2, price)] * n


def test_smart_stop_never_gives_back_more_than_40_percent_of_best_profit():
    """الخلل الحقيقي: ATR كبير يبعد الوقف الذكي عشرات الدولارات فيبقى الوقف عند الدخول+1$ رغم ربح 25–30$."""
    from signalbot import manage
    big_atr = [(100.0 + 4 * (i % 2) + 6, 100.0 + 4 * (i % 2) - 6, 100.0) for i in range(60)]   # ATR≈12
    for gain in (10.5, 18.0, 28.0):
        stop, state = manage.smart_stop("buy", 100.0, 100.0 + gain, 1.0, 0.01, big_atr)
        assert stop >= 100.0 + 0.6 * gain - 1e-9, (gain, stop)
        stop_s, _ = manage.smart_stop("sell", 100.0, 100.0 - gain, 1.0, 0.01, big_atr)
        assert stop_s <= 100.0 - 0.6 * gain + 1e-9


def test_trade_that_ran_28_dollars_is_not_stopped_at_plus_one():
    ex = MEx()
    ex.structure_bars = lambda sym=None: [(106.0, 94.0, 100.0)] * 60           # ATR≈12: هيكلياً الوقف بعيد جداً
    j = Journal(tempfile.mkdtemp())
    m = ManualManager(ex, j)
    m.tick(set(), 1)
    ex.bars = ([(106.0, 99.0)], 2, 105.0); m.tick(set(), 2)
    ex.pos[0]["stopLoss"] = ex.mods[-1][1]
    ex.bars = ([(128.0, 104.0)], 3, 127.0); m.tick(set(), 3)
    assert ex.mods[-1][1] >= 100.0 + 0.6 * 28 - 1e-9                          # ≥ +16.8$ لا +1$
