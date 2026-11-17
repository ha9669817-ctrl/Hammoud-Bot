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
    def size(self, d, p): return 0.01, 0.07, 1000.0
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
        self.pos = [{"ticket": 7, "symbol": "XAUUSDm", "side": "BUY", "openPrice": 100.0, "stopLoss": 90.0}]
        self.mods, self.bars = [], ([], 1, 100.0)

    def positions_raw(self): return self.pos
    def bars_for(self, sym, since): return self.bars
    def modify_sl(self, t, sl): self.mods.append((t, sl))


def test_manual_positions_adopted_and_trailed_many():
    ex = MEx()
    ex.pos.append({"ticket": 8, "symbol": "XAUUSDm", "side": "SELL", "openPrice": 100.0, "stopLoss": 110.0})
    ex.pos.append({"ticket": 9, "symbol": "XAUUSDm", "side": "BUY", "openPrice": 100.0})
    j = Journal(tempfile.mkdtemp())
    m = ManualManager(ex, j)
    m.tick(set(), 1)
    assert not ex.mods
    ex.bars = ([(111.0, 99.0)], 2, 108.0)
    m.tick(set(), 2)
    assert (7, 101.0) in ex.mods and len(ex.mods) == 1
    ex.pos[0]["stopLoss"] = 101.0
    ex.bars = ([(104.0, 88.0)], 3, 92.0)
    m.tick(set(), 3)
    assert any(t == 8 for t, _ in ex.mods)
    assert not any(t == 9 for t, _ in ex.mods)
    assert any(e.get("event") == "adopt_no_sl" for e in j.read("events.jsonl"))


def test_bot_own_tickets_are_not_touched_by_manual_manager():
    ex = MEx(); ex.bars = ([(130.0, 99.0)], 2, 125.0)
    m = ManualManager(ex, Journal(tempfile.mkdtemp()))
    m.tick({"7"}, 1)
    assert not ex.mods


def test_normalize_unreadable_returns_none():
    assert normalize({"foo": 1}) is None
