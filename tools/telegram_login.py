"""شغّله على جهازك مرة واحدة لتوليد TG_SESSION.

  pip install telethon
  python tools/telegram_login.py

يطلب منك api_id و api_hash (من my.telegram.org ← API development tools) ثم رقم هاتفك والرمز.
ينتج نصاً طويلاً: ضعه في Render كمتغير بيئة باسم TG_SESSION. لا ترسله لأحد (يعادل كلمة مرور حسابك).
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("api_id: ").strip())
api_hash = input("api_hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nانسخ السطر التالي إلى Render (TG_SESSION):\n")
    print(client.session.save())
