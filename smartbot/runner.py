"""حلقة التشغيل الحية: تغذي البوت بالشموع المكتملة، وتفتح الصفقات عبر المنفذ، وتسجل الإغلاق من الوسيط.

- fetch_fn() يعيد شموع H1 مكتملة فقط (الأعمدة: time, open, high, low, close).
- executor: TickerAllExecutor (يرفض العمل إلا إذا كان SMARTBOT_MODE=demo).
- لا تُفتح صفقة إلا بإشارة البوت نفسه، وبوقف وهدف يُرسلان مع الأمر.
"""
import pandas as pd

from .engine import SmartBot


class LiveRunner:
    def __init__(self, bot: SmartBot, executor, symbol: str, fetch_fn, comment: str = "smartbot"):
        self.bot = bot
        self.executor = executor
        self.symbol = symbol
        self.fetch_fn = fetch_fn
        self.comment = comment
        self.last_ts = None
        self.bot.external = True
        self.bot.on_entry = self._on_entry

    def _on_entry(self, pos: dict):
        side = "buy" if pos["side"] == 1 else "sell"
        res = self.executor.open(self.symbol, side, pos["entry"], pos["stop"], pos["tgt"],
                                 pos["risk_usd"], self.comment)
        if not res.get("opened"):
            self.bot.j.event("skip", res.get("reason", "رُفض الأمر"), {"symbol": self.symbol})
            return None
        return res["ticket"]

    def start(self) -> dict:
        df = self.fetch_fn()
        res = self.bot.bootstrap(df)
        self.last_ts = str(df["time"].iloc[-1]) if len(df) else None
        return res

    def tick(self) -> list:
        """يُستدعى كل دورة. يرجع الأحداث الجديدة."""
        events: list = []
        df = self.fetch_fn()
        if len(df) == 0:
            return events
        rows = df if self.last_ts is None else df[df["time"].astype(str) > self.last_ts]
        for r in rows.itertuples(index=False):
            events += self.bot.feed(str(r.time), r.open, r.high, r.low, r.close)
            self.last_ts = str(r.time)
        pos = self.bot.pos
        if pos and "ticket" in pos:
            since = pd.Timestamp(pos["entry_ts"]).isoformat() if pos.get("entry_ts") else None
            closed, profit = self.executor.check_closed(pos["ticket"], self.symbol, since)
            if closed and profit is not None:
                self.bot.external_close(str(df["time"].iloc[-1]), profit, events)
                self.bot._save()
        return events
