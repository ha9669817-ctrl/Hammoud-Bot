"""وسيط TickerAll (حساب تجريبي فقط) وأدوات الشموع."""
import csv
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pandas as pd
import requests

TIMEOUT = 20


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


TICKERALL_URL = "https://api.tickerall.com"
TA_COMMENT = "SB"
GOLD_SYMBOLS = ["XAUUSDm", "XAUUSD"]

class TickerAllError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code or ''} {message}".strip())
        self.status, self.code = status, code


def _items(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class TickerAllClient:
    def __init__(self, store, api_key, login, server, password):
        self.store = store
        self.api_key, self.login, self.server, self.password = api_key, login, server, password

    def _req(self, method, path, body=None, params=None, idem=False):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if idem:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        r = requests.request(method, TICKERALL_URL + path, headers=headers, json=body,
                             params=params, timeout=TIMEOUT)
        if r.status_code == 204:
            return None
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400:
            d = data if isinstance(data, dict) else {}
            code = d.get("code") or d.get("error")
            message = d.get("message") or d.get("reason") or r.text[:200]
            raise TickerAllError(r.status_code, code, message)
        return data

    def connect(self) -> dict:
        data = self._req("POST", "/v1/sessions", {
            "broker": "mt5", "server": self.server, "account": int(self.login),
            "password": self.password, "terminalType": "MOBILE"})
        acc = data.get("accountId")
        if not acc:
            raise TickerAllError(502, "NO_ACCOUNT", "لم يُرجع الوسيط معرّف الحساب")
        if data.get("isDemo") is not True:
            try:
                self._req("DELETE", f"/v1/sessions/{acc}")
            except TickerAllError:
                pass
            raise TickerAllError(403, "NOT_DEMO", "الحساب ليس تجريبياً. الحقيقي مقفل في هذه النسخة.")
        session = {"accountId": acc, "expiresAt": data.get("expiresAt"), "connected": utc_now()}
        self.store.meta_set("tickerall_session", session)
        return session

    def _account_id(self):
        sess = self.store.meta_get("tickerall_session")
        if sess and sess.get("accountId"):
            return sess["accountId"]
        return self.connect()["accountId"]

    def _acct(self, method, suffix, body=None, params=None, idem=False):
        for attempt in (1, 2):
            acc = self._account_id()
            try:
                return self._req(method, f"/v1/accounts/{acc}{suffix}", body, params, idem)
            except TickerAllError as e:
                # الاتصال بارد (409) أو الجلسة لم تعد صالحة (404): نعيد الاتصال مرة واحدة فقط
                if attempt == 1 and (e.code == "BROKER_ACCOUNT_NOT_HOT" or e.status == 404):
                    self.connect()
                    continue
                raise

    def account_info(self) -> dict:
        data = self._acct("GET", "")
        info = data.get("account", data) if isinstance(data, dict) else {}
        return {"balance": info.get("balance", info.get("equity")),
                "equity": info.get("equity"), "currency": info.get("currency", "")}

    def symbols(self):
        names = []
        for s in _items(self._acct("GET", "/symbols"), "symbols"):
            names.append(s if isinstance(s, str) else s.get("name", ""))
        return [n for n in names if n]

    def spec(self, symbol):
        for sp in _items(self._acct("GET", "/symbol-specs"), "specs"):
            if sp.get("name") == symbol:
                return sp
        raise TickerAllError(404, "SYMBOL_NOT_FOUND", f"الرمز {symbol} غير موجود في الحساب")

    def positions(self):
        return _items(self._acct("GET", "/positions"), "positions")

    def open_market(self, symbol, side, volume, sl, tp=None):
        body = {"type": "market", "symbol": symbol, "side": "BUY" if side == "buy" else "SELL",
                "volume": volume, "stopLoss": round(sl, 5), "comment": TA_COMMENT}
        if tp:
            body["takeProfit"] = round(tp, 5)
        return self._acct("POST", "/orders", body, idem=True)

    def closed_profit(self, ticket, since_iso):
        """الربح الفعلي لصفقة من سجل الوسيط، بمطابقة رقم التذكرة لا اسم الرمز.
        في MT5 يربط معرّف المركز صفقة الدخول وصفقة الخروج، فنجمع كل صفقات المركز.
        يعيد None إذا لم تظهر صفقة الخروج بعد (لا يُعتبر ذلك ربحاً صفرياً)."""
        want = str(ticket)
        data = self._acct("GET", "/history", params={"from": since_iso, "limit": 500})
        total, has_exit = 0.0, False
        for item in _items(data, "history", "items", "deals"):
            if not isinstance(item, dict):
                continue
            ids = {str(item[k]) for k in HISTORY_ID_KEYS if item.get(k) is not None}
            if want not in ids:
                continue
            entry = str(item.get("entry") or "").upper()
            if item.get("closeTime") or "OUT" in entry:
                has_exit = True
            total += float(item.get("profit") or 0) + float(item.get("swap") or 0) \
                + float(item.get("commission") or 0)
        return round(total, 2) if has_exit else None



HISTORY_ID_KEYS = ("positionId", "position_id", "positionTicket", "orderId", "order_id")



def lots_for_risk(spec, risk_money: float, sl_distance: float) -> float:
    """حجم اللوت بحيث تكون الخسارة عند الوقف قريبة من المخاطرة المحددة، مقرّباً لأسفل حسب خطوة الحجم."""
    tick_size = spec.get("tick_size") or spec.get("tickSize")
    tick_value = spec.get("tick_value_account") or spec.get("tickValueAccount")
    contract = spec.get("contract_size") or spec.get("contractSize")
    if tick_size and tick_value:
        risk_per_lot = sl_distance / float(tick_size) * float(tick_value)
    elif contract:
        risk_per_lot = sl_distance * float(contract)
    else:
        raise TickerAllError(422, "NO_SPEC", "مواصفات الرمز لا تكفي لحساب الحجم")
    if risk_per_lot <= 0:
        raise TickerAllError(422, "BAD_SL", "مسافة الوقف صفرية")
    step = float(spec.get("volumeStep") or spec.get("volume_step") or 0.01)
    vmin = float(spec.get("volumeMin") or spec.get("volume_min") or step)
    raw = Decimal(str(risk_money / risk_per_lot))
    lots = float((raw / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))
    return max(lots, vmin)


TA_CANDLE_TF = {"1m": "M1", "1h": "H1", "1d": "D1"}
INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400, "1w": 604800, "1mo": 2592000}


def _ts_any(value):
    """يقبل وقتاً ISO أو رقم ميلي ثانية/ثانية."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    return _parse_ts(value)



# ---------------------------------------------------------------- إضافات البوت الجديد
def env_creds():
    """بيانات الحساب من متغيرات بيئة Render فقط (لا تُخزَّن في الكود ولا في قاعدة البيانات)."""
    vals = {k: (os.getenv(k) or "").strip()
            for k in ("TICKERALL_API_KEY", "MT5_SERVER", "MT5_ACCOUNT_ID", "MT5_PASSWORD")}
    if not all(vals.values()) or not vals["MT5_ACCOUNT_ID"].isdigit():
        return None
    return vals


def make_client(store):
    c = env_creds()
    if not c:
        return None
    return TickerAllClient(store, c["TICKERALL_API_KEY"], c["MT5_ACCOUNT_ID"],
                           c["MT5_SERVER"], c["MT5_PASSWORD"])


def _modify_position(client, ticket, sl):
    """تعديل وقف مركز مفتوح. مسار REST غير موثّق في الملفات المتاحة، فنجرّب الصيغ المعقولة
    بالترتيب ونحفظ الناجحة. أي فشل يُرفع كما هو ويسجّله المحرك (لا نخمّن بصمت)."""
    body = {"stopLoss": round(float(sl), 5)}
    known = client.store.meta_get("ta_modify_variant")
    variants = [("PATCH", f"/positions/{ticket}"), ("PUT", f"/positions/{ticket}"),
                ("POST", f"/positions/{ticket}/modify")]
    if known is not None and 0 <= known < len(variants):
        variants = [variants[known]] + [v for i, v in enumerate(variants) if i != known]
    last = None
    for method, path in variants:
        try:
            res = client._acct(method, path, body, idem=True)
            client.store.meta_set("ta_modify_variant", ["PATCH", "PUT", "POST"].index(method))
            return res
        except TickerAllError as e:
            last = e
            if e.status not in (404, 405):
                raise
    raise last


class SignalExecutor:
    """واجهة بوت التوصيات مع الوسيط. تُرفض أي صفقة ما لم يكن SIGNALBOT_MODE=demo."""

    def __init__(self, store, symbol=None):
        self.store = store
        self.client = make_client(store)
        self._symbol = symbol
        if os.getenv("SIGNALBOT_MODE", "").lower() != "demo":
            raise RuntimeError("SIGNALBOT_MODE ليس demo: التنفيذ مقفل")
        if self.client is None:
            raise RuntimeError("بيانات الحساب غير مضبوطة في Render")

    @property
    def symbol(self):
        if not self._symbol:
            names = set(self.client.symbols())
            self._symbol = next((s for s in GOLD_SYMBOLS if s in names), GOLD_SYMBOLS[0])
        return self._symbol

    def size(self, sl_dist, _unused=None):
        """لوت ثابت: الأصغر المسموح (أو SIGNALBOT_LOT إن ضُبط). لا حساب مخاطرة ولا تجاهل بسبب الحجم."""
        spec = self._spec(self.symbol)
        step = float(spec.get("volumeStep") or spec.get("volume_step") or 0.01)
        vmin = float(spec.get("volumeMin") or spec.get("volume_min") or step)
        try:
            want = float(os.getenv("SIGNALBOT_LOT", "") or vmin)
        except ValueError:
            want = vmin
        lots = max(vmin, round(round(want / step) * step, 8))
        try:
            balance = float(self.client.account_info().get("balance") or 0)
        except Exception:
            balance = 0.0
        return lots, round(lots * _risk_per_lot(spec, sl_dist), 2), balance

    def place(self, side, sl, tp, lots):
        res = self.client.open_market(self.symbol, side, lots, sl, tp)
        ticket = (res or {}).get("ticket")
        if ticket is None:
            raise TickerAllError(502, "NO_TICKET", f"الرد بلا رقم تذكرة: {str(res)[:120]}")
        return ticket

    def modify_sl(self, ticket, sl):
        _modify_position(self.client, ticket, sl)

    def open_tickets(self):
        return {p.get("ticket"): "open" for p in self.positions_raw()
                if p.get("symbol") == self.symbol and p.get("ticket") is not None}

    def closed_profit(self, ticket, since_iso):
        return self.client.closed_profit(ticket, since_iso)

    def usd_per_price(self, symbol, lots):
        """كم دولار يربح/يخسر المركز لكل 1.0 حركة في السعر."""
        return _risk_per_lot(self._spec(symbol), 1.0) * float(lots)

    def candle_rows(self, tf, count, symbol=None):
        """[(epoch, open, high, low, close)] الأقدم أولاً، آخرها الشمعة الجارية. للتحليل متعدد الفريمات."""
        symbol = symbol or self.symbol
        mins = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}[tf]
        now = datetime.now(timezone.utc)
        data = self.client._acct("GET", "/candles", params={
            "symbol": symbol, "timeframe": tf, "count": count,
            "from": (now - timedelta(minutes=int(count * mins * 1.8) + 60)).isoformat(), "to": now.isoformat()})
        rows = []
        for c in _items(data, "candles", "data", "items", "bars"):
            if not isinstance(c, dict) or c.get("open") is None:
                continue
            t = _ts_any(c.get("time") or c.get("timestamp") or c.get("open_time") or c.get("t"))
            if t is not None:
                rows.append((t.timestamp(), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])))
        rows.sort()
        return rows[-count:]

    def structure_bars(self, symbol=None, tf="M5", count=80):
        """(high, low, close) لآخر شموع M5 (الأقدم أولاً، والأخيرة جارية). ذاكرة 15 ثانية لتخفيف الطلبات."""
        symbol = symbol or self.symbol
        cache = self.__dict__.setdefault("_struct_cache", {})
        hit = cache.get(symbol)
        if hit and time.time() - hit[0] < 15:
            return hit[1]
        now = datetime.now(timezone.utc)
        mins = count * 5 * 3 + 30   # نافذة أوسع: الوسيط قد يعيد أقل من count (إغلاقات/ثغرات)
        data = self.client._acct("GET", "/candles", params={
            "symbol": symbol, "timeframe": tf, "count": count,
            "from": (now - timedelta(minutes=mins)).isoformat(), "to": now.isoformat()})
        rows = []
        for c in _items(data, "candles", "data", "items", "bars"):
            if not isinstance(c, dict) or c.get("open") is None:
                continue
            t = _ts_any(c.get("time") or c.get("timestamp") or c.get("open_time") or c.get("t"))
            if t is not None:
                rows.append((t.timestamp(), float(c["high"]), float(c["low"]), float(c["close"])))
        rows.sort()
        out = [(h, l, c) for _, h, l, c in rows][-count:]
        cache[symbol] = (time.time(), out)
        return out

    def positions_raw(self):
        """قائمة المراكز مع ذاكرة قصيرة (ثانيتان) حتى لا نكرر الطلب في الدورة الواحدة."""
        c = getattr(self, "_pos_cache", None)
        if c and time.time() - c[0] < 1.5:
            return c[1]
        data = [p for p in self.client.positions() if isinstance(p, dict)]
        self._pos_cache = (time.time(), data)
        return data

    def _spec(self, symbol):
        """مواصفات الرمز لا تتغير: تُطلب مرة واحدة لكل رمز."""
        cache = self.__dict__.setdefault("_spec_cache", {})
        if symbol not in cache:
            cache[symbol] = self.client.spec(symbol)
        return cache[symbol]

    def recent_bars(self, since_ts):
        return self.bars_for(self.symbol, since_ts)

    def bars_for(self, symbol, since_ts):
        """(high, low) لشموع الدقيقة منذ since_ts (ثوانٍ). يعيد (bars, آخر_وقت, آخر_إغلاق)."""
        now = datetime.now(timezone.utc)
        # طلب صغير في الحالة العادية (فحص كل 3 ثوانٍ)، وأكبر فقط لتعويض فترة توقف
        gap_min = int((now.timestamp() - since_ts) // 60) + 3 if since_ts else 120
        minutes = max(5, min(120, gap_min))
        data = self.client._acct("GET", "/candles", params={
            "symbol": symbol, "timeframe": "M1", "count": minutes,
            "from": (now - timedelta(minutes=minutes)).isoformat(), "to": now.isoformat()})
        rows = []
        for c in _items(data, "candles", "data", "items", "bars"):
            if not isinstance(c, dict) or c.get("open") is None:
                continue
            t = _ts_any(c.get("time") or c.get("timestamp") or c.get("open_time") or c.get("t"))
            if t is not None:
                rows.append((t.timestamp(), float(c["high"]), float(c["low"]), float(c["close"])))
        rows.sort()
        if not rows:
            return [], since_ts, None
        # نعيد معالجة آخر شمعة (>=) لأن مداها يتسع وهي تتشكل؛ المعالجة آمنة عند التكرار
        bars = [(h, l) for t, h, l, _ in rows if t >= (since_ts or 0)]
        return bars, rows[-1][0], rows[-1][3]


def _risk_per_lot(spec, sl_distance):
    tick_size = spec.get("tick_size") or spec.get("tickSize")
    tick_value = spec.get("tick_value_account") or spec.get("tickValueAccount")
    contract = spec.get("contract_size") or spec.get("contractSize")
    if tick_size and tick_value:
        return sl_distance / float(tick_size) * float(tick_value)
    return sl_distance * float(contract or 100)
