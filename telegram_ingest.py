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
        self.state = {"running": False, "error": None, "last_message_at": None}
        self._thread = None

    def start(self):
        creds = credentials()
        if creds is None:
            self.state["error"] = "TG_API_ID / TG_API_HASH / TG_SESSION غير مضبوطة في Render"
            return False
        self._thread = threading.Thread(target=self._run, args=creds, daemon=True)
        self._thread.start()
        return True

    def _run(self, api_id, api_hash, session):
        try:
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError:
            self.state["error"] = "حزمة telethon غير مثبتة"
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            client = TelegramClient(StringSession(session), api_id, api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                self.state["error"] = "جلسة تيليجرام منتهية: أعد توليد TG_SESSION"
                return
            self.state.update(running=True, error=None)

            @client.on(events.NewMessage(chats=list(self.channels)))
            async def handler(ev):
                name = self.channels.get(ev.chat_id) or str(ev.chat_id)
                self.state["last_message_at"] = ev.message.date.isoformat()
                self.on_message(name, ev.message.id, ev.message.message or "")

            await client.run_until_disconnected()

        while True:
            try:
                loop.run_until_complete(main())
            except Exception as e:  # انقطاع الشبكة: نعيد المحاولة
                self.state["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            self.state["running"] = False
            import time
            time.sleep(30)
