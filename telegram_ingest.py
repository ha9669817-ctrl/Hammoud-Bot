"""استقبال رسائل قنوات تيليجرام بحساب المستخدم نفسه (Telethon).

القنوات ليست ملكك، فالبوت العادي لا يقرؤها؛ لذلك نستخدم جلسة حسابك (StringSession) المضبوطة
كمتغير بيئة TG_SESSION في Render. الجلسة تعادل كلمة مرور حسابك: لا تضعها في الكود ولا في المحادثة.
"""
import asyncio
import os
import threading

DEFAULT_CHANNELS = ("Free Forex Signals=-1001695031756,Master blue=-1003696057640,"
                    "Liquidity Trader=-1003754368542,Trade Sniper=-1004312056334")


def parse_channels(raw: str) -> dict:
    """«الاسم=المعرّف,الاسم2=المعرّف2» ← {معرّف: اسم}"""
    out = {}
    for part in (raw or DEFAULT_CHANNELS).split(","):
        if "=" not in part:
            continue
        name, cid = part.rsplit("=", 1)
        cid = cid.strip()
        if cid.lstrip("-").isdigit():
            out[int(cid)] = name.strip()
    return out


def credentials():
    api_id, api_hash, session = (os.getenv(k, "").strip() for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"))
    if not (api_id.isdigit() and api_hash and session):
        return None
    return int(api_id), api_hash, session


class TelegramIngest:
    def __init__(self, on_message, channels: dict):
        self.on_message, self.channels = on_message, channels
        self.state = {"running": False, "error": None, "stage": "لم يبدأ", "last_message_at": None,
                      "missing": [], "received": 0}
        self._thread = None

    def start(self):
        creds = credentials()
        if creds is None:
            missing = [k for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION") if not os.getenv(k, "").strip()]
            bad_id = os.getenv("TG_API_ID", "").strip() and not os.getenv("TG_API_ID", "").strip().isdigit()
            self.state["error"] = ("ناقص في Render: " + ", ".join(missing)) if missing else \
                ("TG_API_ID يجب أن يكون أرقاماً فقط" if bad_id else "إعدادات تيليجرام غير صالحة")
            self.state["stage"] = "غير مضبوط"
            return False
        self.state["stage"] = "يبدأ الاتصال"
        self._thread = threading.Thread(target=self._run, args=creds, daemon=True)
        self._thread.start()
        return True

    def _run(self, api_id, api_hash, session):
        try:
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError:
            self.state.update(error="حزمة telethon غير مثبتة على الخادم", stage="فشل")
            return
        import time
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            client = TelegramClient(StringSession(session), api_id, api_hash)
            self.state["stage"] = "يتصل بخوادم تيليجرام"
            await asyncio.wait_for(client.connect(), 40)
            if not await client.is_user_authorized():
                self.state.update(error="جلسة تيليجرام غير صالحة أو منتهية: أعد توليد TG_SESSION", stage="فشل")
                await client.disconnect()
                return
            # الجلسة الجديدة لا تعرف القنوات: بدون تحميل المحادثات لا تُطابَق معرّفات القنوات ولا تصل رسائلها
            self.state["stage"] = "يحمّل قائمة قنواتك"
            known = set()
            async for d in client.iter_dialogs():
                known.add(d.id)
            self.state["missing"] = [n for cid, n in self.channels.items() if cid not in known]

            @client.on(events.NewMessage(chats=list(self.channels)))
            async def handler(ev):
                name = self.channels.get(ev.chat_id) or str(ev.chat_id)
                self.state["last_message_at"] = ev.message.date.isoformat()
                self.state["received"] += 1
                print(f"[telegram] رسالة من {name} #{ev.message.id}", flush=True)
                self.on_message(name, ev.message.id, ev.message.message or "")

            self.state.update(running=True, error=None, stage="متصل ويراقب القنوات")
            print("[telegram] متصل، يراقب:", list(self.channels.values()), flush=True)
            await client.run_until_disconnected()

        while True:
            try:
                loop.run_until_complete(main())
            except asyncio.TimeoutError:
                self.state["error"] = "انتهت مهلة الاتصال بتيليجرام (40 ثانية)"
            except Exception as e:  # انقطاع الشبكة أو خطأ في الجلسة: نعيد المحاولة
                self.state["error"] = f"{type(e).__name__}: {str(e)[:140]}"
            print("[telegram] انقطع:", self.state["error"], flush=True)
            self.state.update(running=False, stage="إعادة المحاولة بعد 30 ثانية")
            time.sleep(30)
