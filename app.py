"""موقع بوت التوصيات: توصيات قنوات تيليجرام ← دخول ← وقف متحرك ← تعلّم لكل قناة (حساب تجريبي فقط)."""
import hmac
import os
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request

import history
from broker import SignalExecutor, make_client
from signalbot.adopt import ManualManager
from signalbot.engine import Journal, SignalBot
from signalbot.parser import parse_signal
from signalbot.runner import Runner
from store import Store
from telegram_ingest import TelegramIngest, parse_channels

ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "data" / "bot.db"))
DATA_DIR = Path(DB_PATH).resolve().parent
HIST_DIR = DATA_DIR / "history"

app = Flask(__name__)
store = Store(DB_PATH)
CHANNELS = parse_channels(os.getenv("TG_CHANNELS", ""))

SB = {"bot": None, "runner": None, "tg": None, "error": None}
_BOOT_LOCK = threading.Lock()


def is_on() -> bool:
    return bool(store.meta_get("signalbot_on", False))


def _authorized() -> bool:
    expected = os.getenv("ADMIN_PASSWORD", "")
    given = request.headers.get("X-Admin-Password", "")
    return bool(expected) and hmac.compare_digest(given, expected)


def _deny():
    return jsonify({"error": "كلمة المرور غير صحيحة"}), 401


def boot():
    """يشغّل البوت والاستقبال مرة واحدة. أي فشل يُحفظ ويظهر في اللوحة بدل أن يوقف الموقع."""
    with _BOOT_LOCK:
        if SB["bot"] is not None:
            return
        try:
            executor = SignalExecutor(store)
            bot = SignalBot(Journal(DATA_DIR / "signalbot"), executor, CHANNELS.values(), is_enabled=is_on)
            runner = Runner(bot, executor, ManualManager(executor, bot.j))
            runner.start()
            SB.update(bot=bot, runner=runner, error=None)
            tg = TelegramIngest(runner.push, CHANNELS)
            tg.start()
            SB["tg"] = tg
        except Exception as e:
            SB["error"] = f"{type(e).__name__}: {str(e)[:200]}"


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/")
def index():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html")


def _account():
    c = make_client(store)
    if c is None:
        return {"error": "بيانات الحساب غير مضبوطة في Render"}
    cache = _account.cache
    if time.time() - cache["at"] < 20 and cache["data"]:
        return cache["data"]
    try:
        info = c.account_info()
        data = {"balance": info.get("balance"), "equity": info.get("equity"), "currency": info.get("currency")}
    except Exception as e:
        data = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    _account.cache = {"at": time.time(), "data": data}
    return data


_account.cache = {"at": 0.0, "data": None}


@app.get("/api/state")
def state():
    bot, runner, tg = SB["bot"], SB["runner"], SB["tg"]
    out = {
        "on": is_on(),
        "mode_ready": os.getenv("SIGNALBOT_MODE", "").lower() == "demo",
        "boot_error": SB["error"],
        "channels": list(CHANNELS.values()),
        "account": _account() if os.getenv("DISABLE_SCHEDULER") != "1" else None,
        "runner": runner.state if runner else None,
        "telegram": tg.state if tg else {"running": False, "error": SB["error"] or "لم يبدأ بعد", "stage": "لم يبدأ"},
        "version": (os.getenv("RENDER_GIT_COMMIT") or "")[:7] or "محلي",
        "stats": bot.channel_stats() if bot else {},
        "open": list(bot.trades.values()) if bot else [],
        "events": bot.j.read("events.jsonl", last=40)[::-1] if bot else [],
        "trades": bot.j.read("trades.jsonl", last=30)[::-1] if bot else [],
        "lessons": bot.j.lessons_text(25) if bot else "",
    }
    return jsonify(out)


@app.post("/api/signalbot/toggle")
def toggle():
    if not _authorized():
        return _deny()
    on = bool((request.get_json(silent=True) or {}).get("on"))
    if on and os.getenv("SIGNALBOT_MODE", "").lower() != "demo":
        return jsonify({"error": "SIGNALBOT_MODE ليس demo في Render: التنفيذ مقفل"}), 400
    store.meta_set("signalbot_on", on)
    if on:
        boot()
    return jsonify({"ok": True, "on": on, "boot_error": SB["error"]})


@app.post("/api/signalbot/resume")
def resume():
    if not _authorized():
        return _deny()
    ch = (request.get_json(silent=True) or {}).get("channel", "")
    if SB["bot"] is None:
        return jsonify({"error": "البوت لم يبدأ"}), 400
    SB["bot"].resume(ch)
    return jsonify({"ok": True})


@app.post("/api/signalbot/message")
def manual_message():
    """لصق توصية يدوياً (بديل أو اختبار): تمر بنفس القواعد تماماً."""
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    text, channel = str(body.get("text", "")), str(body.get("channel", ""))
    if channel not in CHANNELS.values():
        return jsonify({"error": "اسم قناة غير معروف", "channels": list(CHANNELS.values())}), 400
    sig = parse_signal(text, channel)
    if not sig:
        return jsonify({"ok": False, "parsed": None, "note": "النص ليس توصية ذهب مكتملة"})
    if body.get("dry"):
        return jsonify({"ok": True, "parsed": sig, "note": "تحليل فقط، لم يُرسل أمر"})
    if SB["runner"] is None:
        return jsonify({"error": "البوت لم يبدأ: " + str(SB["error"])}), 400
    SB["runner"].push(channel, f"manual-{int(time.time())}", text)
    return jsonify({"ok": True, "parsed": sig, "note": "أُرسلت للبوت وتمر بنفس القواعد"})


# ------------------------------------------------------------------ سحب الشموع
@app.post("/api/history/start")
def history_start():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    client = make_client(store)
    if client is None:
        return jsonify({"error": "الوسيط غير مضبوط بعد. أضف بيانات الحساب في Render"}), 400
    intervals = body.get("intervals") or ["1h", "1d"]
    if any(i not in history.HIST_TF for i in intervals):
        return jsonify({"error": "فريمات غير صالحة"}), 400
    try:
        days = int(body.get("days") or 365)
    except (TypeError, ValueError):
        return jsonify({"error": "عدد الأيام غير صالح"}), 400
    if not 1 <= days <= 400:
        return jsonify({"error": "عدد الأيام يجب أن يكون بين 1 و 400"}), 400
    symbols = body.get("symbols") or ["XAUUSDm"]
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    if not history.begin(client, HIST_DIR, symbols, intervals, days):
        return jsonify({"error": "يوجد سحب قيد التنفيذ"}), 409
    return jsonify({"ok": True})


@app.get("/api/history/status")
def history_status():
    if not _authorized():
        return _deny()
    return jsonify(history.snapshot(HIST_DIR))


@app.get("/api/history/zip")
def history_zip():
    if not _authorized():
        return _deny()
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(HIST_DIR.glob("*.csv")) if HIST_DIR.exists() else []:
            z.write(f, arcname=f.name)
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=history.zip"})


KEEPALIVE_S = 240   # أقل بكثير من 15 دقيقة (مهلة نوم Render المجاني)


def keepalive_once() -> bool:
    """يزور /health عبر العنوان العام ليُحسب ذلك طلباً واردًا فلا ينام الموقع."""
    base = (os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base:
        return False
    try:
        import requests
        requests.get(base + "/health", timeout=15)
        return True
    except Exception:
        return False


def _keepalive_loop():
    while True:
        time.sleep(KEEPALIVE_S)
        keepalive_once()


if os.getenv("DISABLE_SCHEDULER") != "1":
    # يُشغَّل بعد استيراد الوحدة حتى لا يؤخر استجابة /health
    threading.Thread(target=boot, daemon=True).start()
    threading.Thread(target=_keepalive_loop, daemon=True).start()
