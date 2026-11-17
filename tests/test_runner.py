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
