"""حلقة التشغيل: تستهلك رسائل القنوات من طابور وتتابع الصفقات كل دورة."""
import queue
import threading
import time

TICK_S = 20


class Runner:
    def __init__(self, bot, executor, manual=None):
        self.bot, self.ex, self.manual = bot, executor, manual
        self.inbox = queue.Queue()
        self.price = None
        self.state = {"alive": False, "last_tick": None, "error": None}
        self._stop = threading.Event()

    def push(self, channel, msg_id, text):
        self.inbox.put((channel, msg_id, text))

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def step(self):
        """دورة واحدة (منفصلة لتسهيل الاختبار)."""
        since = self.bot.last_bar_ts
        bars, last_ts, close = self.ex.recent_bars(since)
        if close is not None:
            self.price = close
        if since is None:      # أول تشغيل: لا نعالج شموعاً قديمة
            bars = []
        self.bot.last_bar_ts = last_ts
        live = self.ex.open_tickets()
        # رسائل وردت: نقرّر قبل المتابعة حتى تستخدم أحدث سعر
        while True:
            try:
                ch, mid, text = self.inbox.get_nowait()
            except queue.Empty:
                break
            if self.price is None:
                self.bot.j.event("skip", channel=ch, msg=mid, reason="لا سعر حالي من الوسيط")
                continue
            res = self.bot.on_message(ch, mid, text, self.price)
            self.bot.j.event("decision", channel=ch, msg=mid, result=res)
            live = self.ex.open_tickets()
        self.bot.tick(bars, live, self.price)
        if self.manual is not None and self.bot.is_enabled():
            own = {str(t["ticket"]) for t in self.bot.trades.values() if t.get("ticket") is not None}
            self.manual.tick(own, time.time())

    def _loop(self):
        self.state["alive"] = True
        while not self._stop.is_set():
            try:
                self.step()
                self.state.update(last_tick=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), error=None)
            except Exception as e:
                self.state["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            self._stop.wait(TICK_S)
        self.state["alive"] = False
