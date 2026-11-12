"""مركز التحليل الذاتي: ملف واحد يعمل مباشرة على Render.
المالك والمطور: HAMAD.
يشمل: جلب البيانات، الاختبار التاريخي، البحث التطوري، التنفيذ الورقي والتجريبي، الجدولة، واللوحة."""
import hashlib
import hmac
import json
import math
import os
import random
import faulthandler
import resource
import sys
import sqlite3
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
from flask import Flask, Response, jsonify, request

# =====================================================================
# الإعدادات
# =====================================================================
CONFIG = {
    "assets": ["XAUUSD", "BTCUSDT"],  # مفاتيح داخلية؛ اسم الرمز عند الوسيط في TA_DEFAULT_SYMBOLS
    "start_balance": 1000.0,
    "risk_pct": 1.0,
    "check_every_minutes": 15,
    "evolve_every_hours": 6,
    "evolve_generations": 6,
    "evolve_population": 16,
}
ASSETS = {
    "XAUUSD": {"symbol": "PAXGUSDT", "label": "XAUUSD (عبر PAXG)"},
    "BTCUSDT": {"symbol": "BTCUSDT", "label": "BTCUSDT"},
}
HISTORY_H1 = 3000
HISTORY_D1 = 200
COST_R = 0.05          # عمولة وانزلاق تقديري لكل صفقة (R)
MAX_HOLD = 96          # أقصى عدد شموع للصفقة في الاختبار
MAX_HOLD_HOURS = 96
TREND_EMA = 200
TRAIN_SHARE = 0.7      # 70% تدريب، 30% تحقق
MIN_TRAIN_TRADES = 15
MIN_VAL_TRADES = 30
MIN_VAL_TRADES_BY_ASSET = {"BTCUSDT": 20}  # البتكوين يتداول طوال الأسبوع، فعدد صفقات التحقق أقل
MIN_PF = 1.1
MAX_VAL_DD_R = 15.0
DISCLAIMER = "⚠️ تنفيذ تعليمي، وليس توصية مالية."
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "data" / "bot.db"))
CHAMPION_DIR = Path(DB_PATH).resolve().parent / "champions"


def _champion_file(asset):
    return CHAMPION_DIR / f"{asset}.json"


def _champion_file_write(asset, data):
    """نسخة ملفية من الاستراتيجية المعتمدة، تُستعاد عند إعادة التشغيل دون إعادة التطوير."""
    try:
        path = _champion_file(asset)
        if data is None:
            path.unlink(missing_ok=True)
            return
        CHAMPION_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[champions] تعذر حفظ ملف {asset}: {exc}", flush=True)


SEED_CHAMPIONS_FILE = Path(__file__).resolve().parent / "seed" / "champions.json"


def _seed_champions():
    """استراتيجيات مرفوعة مع المستودع (تصدير من الواجهة ثم حفظ في GitHub)."""
    try:
        data = json.loads(SEED_CHAMPIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def restore_champions(store_obj):
    """عند التشغيل: إذا لم توجد استراتيجية في قاعدة البيانات نستعيدها من ملفات التشغيل ثم من المستودع."""
    restored, seed = [], _seed_champions()
    for asset in ASSETS:
        if store_obj.get_champion(asset):
            continue
        data = None
        path = _champion_file(asset)
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[champions] تعذر قراءة ملف {asset}: {exc}", flush=True)
        if data is None:
            data = seed.get(asset)
        if data:
            store_obj.meta_set(f"champion|{asset}", data)
            restored.append(asset)
    return restored

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
TIMEOUT = 20
CYCLE_TIMEOUT_S = 240      # أقصى مدة لفحص أصل واحد، حتى لا يعلق المحرك
EVOLVE_BUDGET_S = 1800     # أقصى مدة لجولة تطوير واحدة
STUCK_S = 900              # بعد هذه المدة من الفحص المتصل يُسجَّل تشخيص التعطل

# =====================================================================
# تنبيهات تليجرام (لا تُسقط الدورة عند الفشل)
# =====================================================================
def send(text: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[telegram skipped]\n" + text, flush=True)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text}, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        print(f"[telegram error] {exc}", flush=True)

# =====================================================================
# البيانات والمؤشرات
# =====================================================================
RAW_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
               "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
KEEP = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def _to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=RAW_COLUMNS)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df[KEEP].drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


BINANCE_COOLDOWN_S = 1800
_binance_block = {"until": 0.0}


def fetch_history(symbol: str, interval: str, total: int) -> pd.DataFrame:
    chunks, end, remaining = [], None, total
    while remaining > 0:
        if time.time() < _binance_block["until"]:
            raise RuntimeError("Binance محظور مؤقتاً بعد رفض سابق (418/429)، يُتخطى حتى انتهاء المهلة")
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end is not None:
            params["endTime"] = end
        r = requests.get(BINANCE_URL, params=params, timeout=TIMEOUT)
        if r.status_code in (418, 429):
            _binance_block["until"] = time.time() + BINANCE_COOLDOWN_S
            raise RuntimeError(f"Binance رفض الطلب ({r.status_code})، إيقاف مؤقت 30 دقيقة")
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        chunks.append(rows)
        end = rows[0][0] - 1
        remaining -= len(rows)
        if len(rows) < limit:
            break
    rows = [row for chunk in reversed(chunks) for row in chunk]
    if not rows:
        raise RuntimeError(f"لا توجد بيانات من Binance للرمز {symbol}")
    return _to_df(rows)


def completed_only(df: pd.DataFrame) -> pd.DataFrame:
    now = pd.Timestamp.now(tz="UTC")
    return df[df["close_time"] <= now].reset_index(drop=True)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()

# =====================================================================
# الاستراتيجية الأساسية: قمة/قاع الأمس + السحب + التأكيد
# =====================================================================
def _daily_levels(daily: pd.DataFrame) -> dict:
    return {row["open_time"].date(): (row["high"], row["low"]) for _, row in daily.iterrows()}


def sweep_run(h1: pd.DataFrame, daily: pd.DataFrame, params: dict):
    from datetime import timedelta
    atr_len, depth, sl_buf, target_r = params["atr_len"], params["depth_mult"], params["sl_buffer"], params["target_r"]
    levels = _daily_levels(daily)
    h1 = h1.copy()
    h1["atr"] = atr(h1, atr_len)

    signals, current_day = [], None
    used_high = used_low = False
    pend, pend_hi, pend_lo = 0, None, None

    for _, row in h1.iterrows():
        day = row["open_time"].date()
        if day != current_day:
            current_day, used_high, used_low = day, False, False
        prev_levels = levels.get(day - timedelta(days=1))
        a, close = row["atr"], row["close"]

        side, ref = None, None
        if pend == 1 and pend_hi is not None and close > pend_hi and not used_low:
            side, ref, used_low = "buy", pend_lo, True
        elif pend == -1 and pend_lo is not None and close < pend_lo and not used_high:
            side, ref, used_high = "sell", pend_hi, True

        if side is not None and not pd.isna(a):
            if side == "buy":
                sl = ref - a * sl_buf
                risk = close - sl
                tp = close + risk * target_r
            else:
                sl = ref + a * sl_buf
                risk = sl - close
                tp = close - risk * target_r
            if risk > 0:
                signals.append({
                    "side": side, "close_ts": row["close_time"],
                    "open_ms": int(row["open_time"].timestamp() * 1000),
                    "entry": float(close), "stop": float(sl), "target": float(tp),
                    "risk": float(risk),
                    "source": "قاع الأمس + سحب + تأكيد" if side == "buy" else "قمة الأمس + سحب + تأكيد",
                })

        pend = 0
        if prev_levels is not None and not pd.isna(a):
            prev_high, prev_low = prev_levels
            sweep_up = row["low"] <= prev_low - a * depth and close > prev_low and not used_low
            sweep_down = row["high"] >= prev_high + a * depth and close < prev_high and not used_high
            if sweep_up:
                pend, pend_hi, pend_lo = 1, row["high"], row["low"]
            elif sweep_down:
                pend, pend_hi, pend_lo = -1, row["high"], row["low"]

    last = h1.iloc[-1]
    last_levels = levels.get(last["open_time"].date() - timedelta(days=1))
    latest = {
        "time": last["close_time"].isoformat(),
        "close": float(last["close"]),
        "atr": None if pd.isna(last["atr"]) else float(last["atr"]),
        "prev_day_high": None if last_levels is None else float(last_levels[0]),
        "prev_day_low": None if last_levels is None else float(last_levels[1]),
        "pending": {1: "شراء بانتظار التأكيد", -1: "بيع بانتظار التأكيد"}.get(pend, "لا يوجد"),
    }
    return signals, latest

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


# =====================================================================
# استراتيجيات المؤشرات (تُكتب هنا بالكامل، بدون كود طرف ثالث)
# كل قاعدة تُنتج إشارات دخول بنفس صيغة sweep_run، فتمر عبر نفس الاختبار
# التاريخي وبوابات التحقق، ولا يُعتمد منها إلا ما ينجح فعلاً.
# =====================================================================
from bots import BOT_FUNCS, BOT_AR  # كتالوج البوتات (bots.py)
import bots as _bots
RULES = ["sweep", "rsi", "macd", "bollinger", "ema_9_21", "ema_50_200", "supertrend",
         "psar", "ao", "heikin", "dual_thrust", "vwap", "london", "bbma", "bb_reentry",
         "donchian", "ema_pullback", "adx_di"]
RULE_AR = {
    "donchian": "اختراق قناة دونشيان 20", "ema_pullback": "ارتداد مع الاتجاه (EMA 21/200)",
    "adx_di": "تقاطع +DI/-DI مع ADX",
    "sweep": "قمة/قاع الأمس + سحب", "rsi": "RSI ارتداد", "macd": "تقاطع MACD",
    "bollinger": "اختراق بولينجر", "ema_9_21": "EMA 9/21", "ema_50_200": "EMA 50/200",
    "supertrend": "Supertrend", "psar": "Parabolic SAR", "ao": "Awesome Oscillator",
    "heikin": "Heikin-Ashi", "dual_thrust": "Dual Thrust", "vwap": "VWAP ارتداد",
    "london": "اختراق نطاق آسيا/لندن", "bbma": "بولنجر + تقاطع MA5/MA10 (BBMA)",
    "bb_reentry": "إعادة دخول بولنجر",
}


RULE_AR.update(BOT_AR)


def _cross_up_down(a: pd.Series, b):
    prev_a, prev_b = a.shift(1), (b.shift(1) if isinstance(b, pd.Series) else b)
    up = (a > b) & (prev_a <= prev_b)
    dn = (a < b) & (prev_a >= prev_b)
    return up.fillna(False).to_numpy(), dn.fillna(False).to_numpy()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _supertrend_dir(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> np.ndarray:
    a = atr(df, n).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    upper, lower = hl2 + mult * a, hl2 - mult * a
    fu, fl = upper.copy(), lower.copy()
    direction = np.zeros(len(df))
    start = int(np.argmax(~np.isnan(a))) if np.any(~np.isnan(a)) else len(df)
    for i in range(start + 1, len(df)):
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] <= 0 and close[i] > fu[i]:
            direction[i] = 1
        elif direction[i - 1] >= 0 and close[i] < fl[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1] if direction[i - 1] != 0 else (1 if close[i] > fl[i] else -1)
    return direction


def _psar_dir(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> np.ndarray:
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    trend = np.zeros(n)
    if n < 3:
        return trend
    up, af, ep, sar = True, step, h[0], l[0]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_step)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, max_step)
        trend[i] = 1 if up else -1
    return trend


def _heikin_green(df: pd.DataFrame) -> np.ndarray:
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    ha_c = (o + h + l + c) / 4
    ha_o = np.zeros(len(df))
    if len(df):
        ha_o[0] = (o[0] + c[0]) / 2
    for i in range(1, len(df)):
        ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2
    return (ha_c > ha_o).astype(float)


def _london_signals(df: pd.DataFrame):
    """نطاق آسيا (00-06 UTC) ثم أول اختراق في 07-11 UTC، مرة واحدة يومياً."""
    n = len(df)
    long_m, short_m = np.zeros(n, bool), np.zeros(n, bool)
    hours = df["open_time"].dt.hour.to_numpy()
    days = df["open_time"].dt.date.to_numpy()
    for day in np.unique(days):
        idx = np.where(days == day)[0]
        asia = idx[hours[idx] < 6]
        london = idx[(hours[idx] >= 7) & (hours[idx] <= 11)]
        if len(asia) == 0 or len(london) == 0:
            continue
        hi, lo = df["high"].to_numpy()[asia].max(), df["low"].to_numpy()[asia].min()
        for j in london:
            c = df["close"].iat[j]
            if c > hi:
                long_m[j] = True
                break
            if c < lo:
                short_m[j] = True
                break
    return long_m, short_m


def _bbma_masks(df: pd.DataFrame):
    """منقول من BBMA (MIT، Ammar Ibrahim) mt5ea.mq5، CheckEntrySignals:
    شراء: إغلاق تحت البولنجر السفلي + MA5(low) يتقاطع فوق MA10(low) + إغلاق فوق EMA50.
    بيع: إغلاق فوق البولنجر العلوي + MA5(high) يتقاطع تحت MA10(high) + إغلاق تحت EMA50.
    MA بسيط، وBollinger بانحراف سكاني (ddof=0) كما في iBands."""
    close, high, low = df["close"], df["high"], df["low"]
    mid = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    lower, upper = mid - 2 * sd, mid + 2 * sd
    ma5h, ma10h = high.rolling(5).mean(), high.rolling(10).mean()
    ma5l, ma10l = low.rolling(5).mean(), low.rolling(10).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    up_low_cross = (ma5l.shift(1) <= ma10l.shift(1)) & (ma5l > ma10l)
    dn_high_cross = (ma5h.shift(1) >= ma10h.shift(1)) & (ma5h < ma10h)
    long_m = ((close < lower) & up_low_cross & (close > ema50)).fillna(False).to_numpy()
    short_m = ((close > upper) & dn_high_cross & (close < ema50)).fillna(False).to_numpy()
    return long_m, short_m


def _bb_reentry_masks(df: pd.DataFrame):
    """منقول من Basic_Bollinger_Bands (MIT، Terence Beaujour) buyflag/sellflag:
    شراء: الشمعة السابقة أغلقت تحت الحد السفلي، والشمعة الحالية فتحت تحته وأغلقت فوقه.
    بيع: العكس عند الحد العلوي. بولنجر 20 وانحراف 2، بانحراف سكاني كما في iBands."""
    o, c = df["open"], df["close"]
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    lo, up = mid - 2 * sd, mid + 2 * sd
    buy = ((o.shift(1) > lo.shift(1)) & (c.shift(1) <= lo.shift(1))
           & (o <= lo) & (c > lo)).fillna(False).to_numpy()
    sell = ((c.shift(1) > up.shift(1)) & (o.shift(1) <= up.shift(1))
            & (o >= up) & (c < up)).fillna(False).to_numpy()
    return buy, sell


def _donchian_masks(df: pd.DataFrame, n: int = 20):
    """اختراق قناة دونشيان: إغلاق يتجاوز أعلى قمة لآخر n شمعة (أو أدنى قاع) لأول مرة.
    مفهوم عام معروف (نظام Turtle)، مكتوب هنا من الصفر."""
    close = df["close"]
    up_lvl = df["high"].rolling(n).max().shift(1)
    dn_lvl = df["low"].rolling(n).min().shift(1)
    up = ((close > up_lvl) & (close.shift(1) <= up_lvl.shift(1))).fillna(False).to_numpy()
    dn = ((close < dn_lvl) & (close.shift(1) >= dn_lvl.shift(1))).fillna(False).to_numpy()
    return up, dn


def _ema_pullback_masks(df: pd.DataFrame):
    """اتجاه بمتوسط 200، ودخول مع عودة السعر فوق EMA21 بعد أن كان تحتها (ارتداد مع الاتجاه).
    شراء: الإغلاق فوق EMA200 وعبر EMA21 صعوداً. بيع: العكس."""
    close = df["close"]
    e21, e200 = _ema(close, 21), _ema(close, 200)
    up = ((close > e21) & (close.shift(1) <= e21.shift(1)) & (close > e200)).fillna(False).to_numpy()
    dn = ((close < e21) & (close.shift(1) >= e21.shift(1)) & (close < e200)).fillna(False).to_numpy()
    return up, dn


def _adx_di_masks(df: pd.DataFrame, n: int = 14, min_adx: float = 20.0):
    """تقاطع +DI و-DI (معادلات Wilder) عندما يكون ADX يدل على وجود اتجاه فعلي."""
    h, l, c = df["high"], df["low"], df["close"]
    up_move, dn_move = h.diff(), -l.diff()
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_w
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_w
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    cross_up, cross_dn = _cross_up_down(pdi, mdi)
    strong = (adx > min_adx).fillna(False).to_numpy()
    return cross_up & strong, cross_dn & strong


def _rule_masks(rule: str, df: pd.DataFrame):
    close = df["close"]
    n = len(df)
    if rule in BOT_FUNCS:
        return BOT_FUNCS[rule](df)
    if rule == "bbma":
        return _bbma_masks(df)
    if rule == "bb_reentry":
        return _bb_reentry_masks(df)
    if rule == "donchian":
        return _donchian_masks(df)
    if rule == "ema_pullback":
        return _ema_pullback_masks(df)
    if rule == "adx_di":
        return _adx_di_masks(df)
    if rule == "rsi":
        r = _rsi(close, 14)
        up, _ = _cross_up_down(r, 30.0)
        _, dn = _cross_up_down(r, 70.0)
        return up, dn
    if rule == "macd":
        macd = _ema(close, 12) - _ema(close, 26)
        sig = _ema(macd, 9)
        return _cross_up_down(macd, sig)
    if rule == "bollinger":
        mid = close.rolling(20).mean()
        sd = close.rolling(20).std()
        up_band, lo_band = mid + 2 * sd, mid - 2 * sd
        up = ((close > up_band) & (close.shift(1) <= up_band.shift(1))).fillna(False).to_numpy()
        dn = ((close < lo_band) & (close.shift(1) >= lo_band.shift(1))).fillna(False).to_numpy()
        return up, dn
    if rule == "ema_9_21":
        return _cross_up_down(_ema(close, 9), _ema(close, 21))
    if rule == "ema_50_200":
        return _cross_up_down(_ema(close, 50), _ema(close, 200))
    if rule == "supertrend":
        d = _supertrend_dir(df)
        prev = np.concatenate([[0], d[:-1]])
        return (d == 1) & (prev == -1), (d == -1) & (prev == 1)
    if rule == "psar":
        d = _psar_dir(df)
        prev = np.concatenate([[0], d[:-1]])
        return (d == 1) & (prev == -1), (d == -1) & (prev == 1)
    if rule == "ao":
        med = (df["high"] + df["low"]) / 2
        ao = med.rolling(5).mean() - med.rolling(34).mean()
        return _cross_up_down(ao, 0.0)
    if rule == "heikin":
        g = _heikin_green(df)
        prev = np.concatenate([[0.0], g[:-1]])
        return (g == 1) & (prev == 0), (g == 0) & (prev == 1)
    if rule == "dual_thrust":
        hh, ll = df["high"].rolling(4).max(), df["low"].rolling(4).min()
        hc, lc = close.rolling(4).max(), close.rolling(4).min()
        rng = np.maximum(hh - lc, hc - ll).shift(1)
        up_lvl = df["open"] + 0.5 * rng
        dn_lvl = df["open"] - 0.5 * rng
        return (close > up_lvl).fillna(False).to_numpy(), (close < dn_lvl).fillna(False).to_numpy()
    if rule == "vwap":
        tp = (df["high"] + df["low"] + close) / 3
        vol = df["volume"]
        vwap = (tp * vol).rolling(48).sum() / vol.rolling(48).sum().replace(0, np.nan)
        a = atr(df, 14)
        lo_b, up_b = vwap - a, vwap + a
        dn = ((close < lo_b) & (close.shift(1) >= lo_b.shift(1))).fillna(False).to_numpy()
        up = ((close > up_b) & (close.shift(1) <= up_b.shift(1))).fillna(False).to_numpy()
        return up, dn
    if rule == "london":
        return _london_signals(df)
    return np.zeros(n, bool), np.zeros(n, bool)


def rule_signals(rule: str, df: pd.DataFrame, atr_stop: float, target_r: float) -> list:
    """نفس صيغة إشارات sweep_run: دخول عند إغلاق الشمعة، وقف بمضاعف ATR، وهدف بنسبة R."""
    long_m, short_m = _rule_masks(rule, df)
    a = atr(df, 14).to_numpy()
    closes = df["close"].to_numpy()
    ms = ms_of(df)
    ct = df["close_time"].to_numpy()
    sigs = []
    for i in range(len(df)):
        if np.isnan(a[i]) or not (long_m[i] or short_m[i]):
            continue
        buy = bool(long_m[i]) and not bool(short_m[i])
        entry = float(closes[i])
        dist = float(a[i]) * atr_stop
        if buy:
            stop = entry - dist
            risk = entry - stop
            tp = entry + risk * target_r
        else:
            stop = entry + dist
            risk = stop - entry
            tp = entry - risk * target_r
        if risk <= 0:
            continue
        sigs.append({"side": "buy" if buy else "sell", "close_ts": ct[i], "open_ms": ms[i],
                     "entry": entry, "stop": float(stop), "target": float(tp), "risk": float(risk),
                     "source": RULE_AR.get(rule, rule)})
    return sigs


# =====================================================================
# الاختبار التاريخي والبحث التطوري
# =====================================================================
SPACE = {
    "rule": RULES,
    "atr_stop": [1.0, 1.5, 2.0],
    "atr_len": [7, 14, 21],
    "depth_mult": [0.05, 0.1, 0.2, 0.3, 0.5],
    "sl_buffer": [0.0, 0.1, 0.2, 0.3],
    "target_r": [1.0, 1.5, 2.0, 2.5, 3.0],
    "tf": [1, 2, 4],
    "trend": [False, True],
}
BASELINE = {"rule": "sweep", "atr_stop": 1.5, "atr_len": 14, "depth_mult": 0.2, "sl_buffer": 0.2, "target_r": 2.0, "tf": 1, "trend": False}


def key(genome: dict) -> str:
    return json.dumps(genome, sort_keys=True)


def normalize(genome: dict) -> dict:
    """يثبت المعاملات غير المستخدمة في كل عائلة، حتى لا تتكرر الجينات المتكافئة."""
    g = dict(genome)
    g.setdefault("rule", "sweep")
    g.setdefault("atr_stop", 1.5)
    if g["rule"] == "sweep":
        g["atr_stop"] = 1.5
    else:
        g.update(atr_len=14, depth_mult=0.2, sl_buffer=0.2, trend=False)
    return g


def random_genome(rng: random.Random) -> dict:
    return normalize({k: rng.choice(v) for k, v in SPACE.items()})


def mutate(genome: dict, rng: random.Random) -> dict:
    child = dict(genome)
    for k in rng.sample(list(SPACE), k=rng.choice([1, 2])):
        child[k] = rng.choice(SPACE[k])
    return normalize(child)


def ms_of(df: pd.DataFrame) -> list:
    return [int(t.timestamp() * 1000) for t in df["open_time"]]


def resample(h1: pd.DataFrame, hours: int) -> pd.DataFrame:
    if hours == 1:
        return h1.reset_index(drop=True)
    g = h1.set_index("open_time").resample(f"{hours}h", label="left", closed="left")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
        "close_time": g["close_time"].last(), "n": g["open"].count(),
    })
    return out[out["n"] == hours].drop(columns="n").reset_index()


def signals_for(genome: dict, h1: pd.DataFrame, daily: pd.DataFrame):
    df = resample(h1, genome["tf"])
    rule = genome.get("rule", "sweep")
    if rule != "sweep":
        sigs = rule_signals(rule, df, genome.get("atr_stop", 1.5), genome["target_r"])
        last = df.iloc[-1]
        a = atr(df, 14).iloc[-1]
        latest = {"time": last["close_time"].isoformat(), "close": float(last["close"]),
                  "atr": None if pd.isna(a) else float(a), "prev_day_high": None,
                  "prev_day_low": None, "pending": "لا يوجد"}
        return df, sigs, latest
    params = {"atr_len": genome["atr_len"], "depth_mult": genome["depth_mult"],
              "sl_buffer": genome["sl_buffer"], "target_r": genome["target_r"]}
    sigs, latest = sweep_run(df, daily, params)
    if genome["trend"] and sigs:
        ema = df["close"].ewm(span=TREND_EMA, adjust=False).mean()
        ema_by_ms = dict(zip(ms_of(df), ema))
        sigs = [s for s in sigs if (s["side"] == "buy") == (s["entry"] > ema_by_ms[s["open_ms"]])]
    return df, sigs, latest


def simulate(genome: dict, df: pd.DataFrame, sigs: list) -> list:
    index = {m: i for i, m in enumerate(ms_of(df))}
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    n = len(df)
    out, busy = [], -1
    for s in sigs:
        i = index.get(s["open_ms"])
        if i is None or i <= busy:
            continue
        buy = s["side"] == "buy"
        stop, tgt, entry, risk = s["stop"], s["target"], s["entry"], s["risk"]
        last = min(i + MAX_HOLD, n - 1)
        r, exit_j = None, None
        for j in range(i + 1, last + 1):
            if buy:
                hit_stop, hit_tgt = lo[j] <= stop, hi[j] >= tgt
            else:
                hit_stop, hit_tgt = hi[j] >= stop, lo[j] <= tgt
            if hit_stop:
                r, exit_j = -1.0, j
                break
            if hit_tgt:
                r, exit_j = genome["target_r"], j
                break
        if r is None:
            if i + MAX_HOLD > n - 1:
                continue
            exit_j = i + MAX_HOLD
            move = (cl[exit_j] - entry) if buy else (entry - cl[exit_j])
            r = move / risk
        out.append((i, r - COST_R))
        busy = exit_j
    return out


def metrics(rs: list) -> dict:
    n = len(rs)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "max_dd_r": 0.0,
                "expectancy_r": 0.0, "total_r": 0.0}
    wins = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r <= 0)
    pf = wins / losses if losses > 0 else 99.0
    eq = np.concatenate([[0.0], np.cumsum(rs)])
    return {"trades": n,
            "win_rate": round(100 * sum(1 for r in rs if r > 0) / n, 1),
            "profit_factor": round(min(pf, 99.0), 2),
            "max_dd_r": round(float(np.max(np.maximum.accumulate(eq) - eq)), 2),
            "expectancy_r": round(sum(rs) / n, 3),
            "total_r": round(sum(rs), 2)}


def evaluate(genome: dict, h1: pd.DataFrame, daily: pd.DataFrame):
    df, sigs, latest = signals_for(genome, h1, daily)
    trades = simulate(genome, df, sigs)
    cut = int(len(df) * TRAIN_SHARE)
    train = [r for i, r in trades if i < cut]
    val = [r for i, r in trades if i >= cut]
    return {"train": metrics(train), "val": metrics(val)}, latest


def passes(res: dict, min_val: int = MIN_VAL_TRADES) -> bool:
    t, v = res["train"], res["val"]
    return (t["trades"] >= MIN_TRAIN_TRADES and t["profit_factor"] >= MIN_PF
            and v["trades"] >= min_val and v["profit_factor"] >= MIN_PF
            and v["max_dd_r"] <= MAX_VAL_DD_R and v["expectancy_r"] > 0)


def score(res: dict) -> float:
    v = res["val"]
    return round(v["expectancy_r"] * math.sqrt(min(v["trades"], 50)), 3)


def run_evolution(asset, h1, daily, store, generations, population, seed=None) -> dict:
    rng = random.Random(seed)
    champ = store.get_champion(asset)
    parents = [champ["genome"]] if champ else []
    seen, accepted, evaluated = set(), [], 0
    deadline = time.time() + EVOLVE_BUDGET_S

    for gen in range(generations):
        if time.time() > deadline:
            break
        candidates = []
        if gen == 0:
            if champ:
                candidates.append(champ["genome"])
            candidates.append(BASELINE)
        while len(candidates) < population:
            if parents and rng.random() < 0.7:
                candidates.append(mutate(rng.choice(parents), rng))
            else:
                candidates.append(random_genome(rng))
        for g in candidates:
            if time.time() > deadline:
                break
            k = key(g)
            if k in seen:
                continue
            seen.add(k)
            res, _ = evaluate(g, h1, daily)
            evaluated += 1
            ok, sc = passes(res, MIN_VAL_TRADES_BY_ASSET.get(asset, MIN_VAL_TRADES)), score(res)
            store.save_strategy(asset, g, "accepted" if ok else "rejected", sc, res)
            if ok:
                accepted.append((sc, g, res))
        parents = [g for _, g, _ in sorted(accepted, key=lambda x: x[0], reverse=True)[:4]]

    store.prune(asset)
    best = max(accepted, key=lambda x: x[0]) if accepted else None
    if best:
        sc, g, res = best
        since = champ["since"] if (champ and champ["genome"] == g) else utc_now()
        store.set_champion(asset, {"genome": g, "score": sc, "result": res, "since": since})
        note = "تم اعتماد استراتيجية"
    else:
        store.set_champion(asset, None)
        note = "لم تنجح أي استراتيجية في الاختبار، التداول متوقف لهذا الأصل"
    store.log_evolution(asset, evaluated, len(accepted), key(best[1]) if best else None, note)
    return {"evaluated": evaluated, "accepted": len(accepted), "note": note,
            "champion": best[1] if best else None}

# =====================================================================
# التخزين SQLite
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS strategies(
    id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT, genome TEXT, status TEXT,
    score REAL, result TEXT, created_utc TEXT);
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT, genome TEXT, side TEXT,
    entry_at INTEGER, entry REAL, stop REAL, target REAL, risk REAL, risk_usd REAL,
    status TEXT, exit_at INTEGER, exit_price REAL, r REAL, pnl_usd REAL,
    opened_utc TEXT, closed_utc TEXT, mode TEXT DEFAULT 'paper', qty REAL, order_id TEXT,
    UNIQUE(asset, entry_at));
CREATE TABLE IF NOT EXISTS evolutions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT, created_utc TEXT,
    evaluated INTEGER, accepted INTEGER, champion TEXT, note TEXT);
"""
MIGRATIONS = {"mode": "TEXT DEFAULT 'paper'", "qty": "REAL", "order_id": "TEXT"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str, start_balance: float):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.path = path
        with self._tx() as c:
            c.executescript(SCHEMA)
            existing = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
            for col, ddl in MIGRATIONS.items():
                if col not in existing:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {col} {ddl}")
        if self.meta_get("balance") is None:
            self.meta_set("balance", start_balance)
        if self.meta_get("mode") is None:
            self.meta_set("mode", "paper")

    @contextmanager
    def _tx(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def meta_get(self, k):
        with self._tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return None if row is None else json.loads(row["value"])

    def meta_set(self, k, v):
        with self._tx() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))

    def balance(self) -> float:
        return float(self.meta_get("balance"))

    def add_balance(self, delta: float) -> None:
        self.meta_set("balance", round(self.balance() + delta, 2))

    def get_mode(self) -> str:
        return self.meta_get("mode") or "paper"

    def set_mode(self, mode: str) -> None:
        self.meta_set("mode", mode)

    def get_assets(self) -> list:
        return self.meta_get("assets") or CONFIG["assets"]

    def set_assets(self, lst: list) -> None:
        self.meta_set("assets", lst)

    def get_broker_creds(self):
        return self.meta_get("broker")

    def set_broker_creds(self, api_key, api_secret, market) -> None:
        self.meta_set("broker", {"api_key": api_key, "api_secret": api_secret, "market": market})

    def clear_broker_creds(self) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM meta WHERE key='broker'")

    def get_champion(self, asset):
        return self.meta_get(f"champion|{asset}")

    def set_champion(self, asset, data) -> None:
        if data is None:
            with self._tx() as c:
                c.execute("DELETE FROM meta WHERE key=?", (f"champion|{asset}",))
        else:
            self.meta_set(f"champion|{asset}", data)
        _champion_file_write(asset, data)

    def save_strategy(self, asset, genome, status, score_v, result) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO strategies(asset,genome,status,score,result,created_utc) VALUES(?,?,?,?,?,?)",
                      (asset, json.dumps(genome, sort_keys=True), status, score_v,
                       json.dumps(result), utc_now()))

    def top_strategies(self, asset, n=8) -> list:
        with self._tx() as c:
            rows = c.execute("SELECT genome, score, result, created_utc FROM strategies "
                             "WHERE asset=? AND status='accepted' ORDER BY score DESC, id DESC LIMIT ?",
                             (asset, n)).fetchall()
        return [{"genome": json.loads(r["genome"]), "score": r["score"],
                 "result": json.loads(r["result"]), "created_utc": r["created_utc"]} for r in rows]

    def prune(self, asset, keep=20) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM strategies WHERE asset=? AND status='rejected'", (asset,))
            c.execute("DELETE FROM strategies WHERE asset=? AND status='accepted' AND id NOT IN ("
                      " SELECT id FROM strategies WHERE asset=? AND status='accepted' "
                      " ORDER BY score DESC, id DESC LIMIT ?)", (asset, asset, keep))

    def log_evolution(self, asset, evaluated, accepted, champion, note) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO evolutions(asset,created_utc,evaluated,accepted,champion,note) VALUES(?,?,?,?,?,?)",
                      (asset, utc_now(), evaluated, accepted, champion, note))

    def recent_evolutions(self, n=12) -> list:
        with self._tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM evolutions ORDER BY id DESC LIMIT ?", (n,)).fetchall()]

    def has_trade(self, asset, entry_at) -> bool:
        with self._tx() as c:
            return c.execute("SELECT 1 FROM trades WHERE asset=? AND entry_at=?",
                             (asset, entry_at)).fetchone() is not None

    def open_trade(self, asset, genome, side, entry_at, entry, stop, target, risk, risk_usd,
                   mode="paper", qty=None, order_id=None):
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO trades(asset,genome,side,entry_at,entry,stop,target,risk,risk_usd,"
                "status,opened_utc,mode,qty,order_id) VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                (asset, genome, side, entry_at, entry, stop, target, risk, risk_usd, utc_now(), mode, qty, order_id))
            return cur.lastrowid if cur.rowcount else None

    def open_trades(self, asset=None) -> list:
        q, args = "SELECT * FROM trades WHERE status='open'", ()
        if asset:
            q, args = q + " AND asset=?", (asset,)
        with self._tx() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY entry_at", args).fetchall()]

    def closed_trades(self) -> list:
        with self._tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM trades WHERE status='closed' ORDER BY exit_at").fetchall()]

    def close_trade(self, trade_id, exit_at, exit_price, r, pnl_usd) -> None:
        with self._tx() as c:
            c.execute("UPDATE trades SET status='closed', exit_at=?, exit_price=?, r=?, pnl_usd=?, closed_utc=? WHERE id=?",
                      (exit_at, exit_price, r, pnl_usd, utc_now(), trade_id))

# =====================================================================
# الوسيط: ورقي، وتجريبي على Binance Testnet (سبوت أو فيوتشرز)
# =====================================================================
MARKETS = {
    "spot": {"label": "سبوت تجريبي (شراء فقط)", "base": "https://testnet.binance.vision",
             "info": "/api/v3/exchangeInfo", "order": "/api/v3/order", "balance": "/api/v3/account"},
    "futures": {"label": "فيوتشرز تجريبي USDT-M (شراء وبيع)", "base": "https://testnet.binancefuture.com",
                "info": "/fapi/v1/exchangeInfo", "order": "/fapi/v1/order", "balance": "/fapi/v2/balance"},
}
TESTNET_SYMBOLS = {"BTCUSDT": "BTCUSDT"}


class BrokerError(Exception):
    pass


class PaperBroker:
    name = "paper"
    supports_short = True

    def place_entry(self, asset, side, risk_usd, entry, stop):
        return {"order_id": None, "qty": None}

    def close_position(self, trade, price):
        return None


class TestnetBroker:
    name = "demo"

    def __init__(self, key, secret, market="spot"):
        if market not in MARKETS:
            raise BrokerError("نوع السوق غير معروف")
        self.key, self.secret, self.market = key, secret, market
        self.cfg = MARKETS[market]
        self._filters_cache = {}

    @property
    def supports_short(self) -> bool:
        return self.market == "futures"

    def _public(self, path, params=None):
        r = requests.get(self.cfg["base"] + path, params=params or {}, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise BrokerError(f"{r.status_code}: {r.text[:200]}")
        return r.json()

    def _signed(self, method, path, params):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = urlencode(params)
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{self.cfg['base']}{path}?{query}&signature={sig}"
        r = requests.request(method, url, headers={"X-MBX-APIKEY": self.key}, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise BrokerError(f"{r.status_code}: {r.text[:200]}")
        return r.json()

    def _symbol(self, asset):
        if asset not in TESTNET_SYMBOLS:
            raise BrokerError(f"{asset} غير متاح على Binance Testnet")
        return TESTNET_SYMBOLS[asset]

    def _filters(self, symbol):
        if symbol not in self._filters_cache:
            info = self._public(self.cfg["info"], {"symbol": symbol})
            f = {x["filterType"]: x for x in info["symbols"][0]["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters_cache[symbol] = (
                Decimal(lot["stepSize"]), Decimal(lot["minQty"]),
                Decimal(notional.get("minNotional", notional.get("notional", "0"))))
        return self._filters_cache[symbol]

    def _balance_usdt(self) -> Decimal:
        data = self._signed("GET", self.cfg["balance"], {})
        if self.market == "spot":
            for b in data["balances"]:
                if b["asset"] == "USDT":
                    return Decimal(b["free"])
        else:
            for b in data:
                if b["asset"] == "USDT":
                    return Decimal(b["availableBalance"])
        return Decimal("0")

    @staticmethod
    def _floor(x: Decimal, step: Decimal) -> Decimal:
        return (x / step).to_integral_value(rounding=ROUND_DOWN) * step

    def account(self) -> dict:
        return {"usdt_free": float(self._balance_usdt()), "market": self.market}

    def place_entry(self, asset, side, risk_usd, entry, stop):
        if side == "sell" and not self.supports_short:
            raise BrokerError("السبوت لا يدعم البيع على المكشوف. اختر فيوتشرز تجريبي.")
        symbol = self._symbol(asset)
        step, min_qty, min_notional = self._filters(symbol)
        rpu = Decimal(str(abs(entry - stop)))
        if rpu <= 0:
            raise BrokerError("مسافة الوقف صفرية")
        qty = Decimal(str(risk_usd)) / rpu
        max_qty = self._balance_usdt() / Decimal(str(entry))
        qty = self._floor(min(qty, max_qty), step)
        if qty < min_qty or qty * Decimal(str(entry)) < min_notional:
            raise BrokerError("الكمية أقل من حد المنصة الأدنى، الرصيد التجريبي صغير أو الوقف واسع")
        data = self._signed("POST", self.cfg["order"], {
            "symbol": symbol, "side": "BUY" if side == "buy" else "SELL",
            "type": "MARKET", "quantity": format(qty, "f")})
        filled = Decimal(str(data.get("executedQty", "0")))
        if filled <= 0:
            raise BrokerError("لم يُنفَّذ الأمر على المنصة")
        return {"order_id": str(data["orderId"]), "qty": float(filled)}

    def close_position(self, trade, price):
        qty = trade.get("qty")
        if not qty:
            return None
        symbol = self._symbol(trade["asset"])
        step, _, _ = self._filters(symbol)
        params = {"symbol": symbol, "side": "SELL" if trade["side"] == "buy" else "BUY",
                  "type": "MARKET", "quantity": format(self._floor(Decimal(str(qty)), step), "f")}
        if self.market == "futures":
            params["reduceOnly"] = "true"
        data = self._signed("POST", self.cfg["order"], params)
        return str(data.get("orderId"))


def broker_for(store: Store, mode: str):
    if mode == "paper":
        return PaperBroker()
    if mode == "demo":
        creds = store.get_broker_creds()
        if not creds:
            raise BrokerError("أدخل مفاتيح التجربة من صفحة الإعدادات أولاً")
        return TestnetBroker(creds["api_key"], creds["api_secret"], creds["market"])
    raise BrokerError("وضع غير معروف")

# =====================================================================
# الصفقات الورقية ومنطق الإغلاق
# =====================================================================
def risk_usd(store: Store) -> float:
    return round(store.balance() * CONFIG["risk_pct"] / 100, 2)


MAX_OPEN_TOTAL = 3          # أقصى عدد صفقات مفتوحة عبر كل الأصول
DAILY_LOSS_PCT = 3.0        # إيقاف فتح الصفقات عند خسارة يومية تتجاوز هذه النسبة من الرصيد


def daily_pnl(store: Store) -> float:
    """مجموع أرباح وخسائر الصفقات المغلقة منذ منتصف الليل بتوقيت UTC."""
    now = datetime.now(timezone.utc)
    start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    return sum((t["pnl_usd"] or 0.0) for t in store.closed_trades() if (t["exit_at"] or 0) >= start_ms)


def can_open(store: Store, asset, entry_at) -> bool:
    if store.balance() <= 0:
        return False
    if store.open_trades(asset):
        return False
    if len(store.open_trades()) >= MAX_OPEN_TOTAL:
        return False
    if daily_pnl(store) <= -store.balance() * DAILY_LOSS_PCT / 100:
        return False
    return not store.has_trade(asset, entry_at)


def record_open(store: Store, asset, genome_key, s, mode, qty=None, order_id=None):
    entry_at = int(s["close_ts"].timestamp() * 1000)
    usd = risk_usd(store)
    trade_id = store.open_trade(asset, genome_key, s["side"], entry_at, s["entry"], s["stop"],
                                s["target"], s["risk"], usd, mode=mode, qty=qty, order_id=order_id)
    if trade_id is None:
        return None
    return {"id": trade_id, "asset": asset, "side": s["side"], "entry": s["entry"], "stop": s["stop"],
            "target": s["target"], "risk_usd": usd, "mode": mode, "qty": qty, "order_id": order_id}


def _check(t, candles):
    buy = t["side"] == "buy"
    r_target = abs(t["target"] - t["entry"]) / t["risk"]
    max_ms = MAX_HOLD_HOURS * 3600 * 1000
    for row in candles.itertuples(index=False):
        if row.open_ms < t["entry_at"]:
            continue
        if buy:
            hit_stop, hit_tgt = row.low <= t["stop"], row.high >= t["target"]
        else:
            hit_stop, hit_tgt = row.high >= t["stop"], row.low <= t["target"]
        if hit_stop:
            return row.close_ms, t["stop"], -1.0 - COST_R
        if hit_tgt:
            return row.close_ms, t["target"], r_target - COST_R
        if row.open_ms - t["entry_at"] >= max_ms:
            move = (row.close - t["entry"]) if buy else (t["entry"] - row.close)
            return row.close_ms, row.close, move / t["risk"] - COST_R
    return None


def update_open(store: Store, asset, candles, on_close=None):
    closed, errors = [], []
    for t in store.open_trades(asset):
        if t.get("mode") == "tickerall":
            continue
        res = _check(t, candles)
        if res is None:
            continue
        exit_at, price, r = res
        if on_close is not None:
            try:
                on_close(t, price)
            except Exception as exc:
                errors.append(str(exc))
                continue
        pnl = round(r * t["risk_usd"], 2)
        store.close_trade(t["id"], exit_at, price, round(r, 3), pnl)
        store.add_balance(pnl)
        closed.append({**t, "exit_price": price, "r": round(r, 2), "pnl_usd": pnl})
    return closed, errors

# =====================================================================
# وسيط TickerAll: حسابات MT5 تجريبية عبر API. الوقف والهدف يُحملان على جهة الوسيط.
# =====================================================================
TICKERALL_URL = "https://api.tickerall.com"
TA_DEFAULT_SYMBOLS = {"XAUUSD": "XAUUSDm", "BTCUSDT": "BTCUSDm"}
TA_PROFIT_RETRIES = 10
TA_PROFIT_NOW_TRIES = 3   # محاولات فورية داخل الدورة نفسها قبل التأجيل
TA_PROFIT_WAIT_S = 2
TA_SYMBOL_CANDIDATES = {"XAUUSD": ["XAUUSDm", "XAUUSD"], "BTCUSDT": ["BTCUSDm", "BTCUSD"]}
TA_COMMENT = "MA"


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

    def open_market(self, symbol, side, volume, sl, tp):
        return self._acct("POST", "/orders", {
            "type": "market", "symbol": symbol, "side": "BUY" if side == "buy" else "SELL",
            "volume": volume, "stopLoss": round(sl, 5), "takeProfit": round(tp, 5),
            "comment": TA_COMMENT}, idem=True)

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


TA_ENV_KEYS = ("TICKERALL_API_KEY", "MT5_SERVER", "MT5_ACCOUNT_ID", "MT5_PASSWORD")


def ta_env_creds():
    """Credentials from Render environment variables (never stored in code)."""
    vals = {k: (os.getenv(k) or "").strip() for k in TA_ENV_KEYS}
    if not all(vals.values()) or not vals["MT5_ACCOUNT_ID"].isdigit():
        return None
    return {"api_key": vals["TICKERALL_API_KEY"], "login": vals["MT5_ACCOUNT_ID"],
            "server": vals["MT5_SERVER"], "password": vals["MT5_PASSWORD"],
            "symbols": {}, "source": "env"}


def ta_creds(store):
    saved = store.meta_get("tickerall")
    if saved:
        return {**saved, "source": "site"}
    return ta_env_creds()


def ta_client(store):
    c = ta_creds(store)
    if not c:
        return None
    return TickerAllClient(store, c["api_key"], c["login"], c["server"], c["password"])


def ta_symbol(store, asset):
    c = ta_creds(store) or {}
    return (c.get("symbols") or {}).get(asset) or (store.meta_get("ta_symbols") or {}).get(asset) \
        or TA_DEFAULT_SYMBOLS[asset]


def resolve_ta_symbols(store, client) -> dict:
    """يختار اسم الرمز الذي يقبله الوسيط فعلاً (مع اللاحقة m أو بدونها) ويحفظه."""
    found = {}
    for asset, candidates in TA_SYMBOL_CANDIDATES.items():
        for name in candidates:
            try:
                client.spec(name)
                found[asset] = name
                break
            except TickerAllError:
                continue
            except Exception as exc:
                log_event("warn", f"تعذر التحقق من الرمز {name}: {str(exc)[:100]}")
                break
        if asset not in found:
            log_event("warn", f"لم يُعثر على رمز مطابق لـ {asset} عند الوسيط، سيُستخدم {TA_DEFAULT_SYMBOLS[asset]}")
    if found:
        store.meta_set("ta_symbols", found)
        log_event("info", "رموز الوسيط: " + ", ".join(f"{a}={n}" for a, n in found.items()))
    return found


def ta_sync(asset, store, client, now_ms):
    """يغلق في النظام الصفقات التي أُغلقت على المنصة (بالوقف أو الهدف أو يدوياً)."""
    open_ta = [t for t in store.open_trades(asset) if t["mode"] == "tickerall"]
    if not open_ta:
        return []
    tickets = {str(p.get("ticket")) for p in client.positions()}
    closed = []
    for t in open_ta:
        if t["order_id"] in tickets:
            continue
        raw = None
        for attempt in range(TA_PROFIT_NOW_TRIES):
            raw = client.closed_profit(t["order_id"], t["opened_utc"])
            if raw is not None or attempt == TA_PROFIT_NOW_TRIES - 1:
                break
            time.sleep(TA_PROFIT_WAIT_S)  # الوسيط يسجّل الخروج بعد لحظات أحياناً
        key = f"ta_pending|{t['id']}"
        if raw is None:
            tries = int(store.meta_get(key) or 0) + 1
            store.meta_set(key, tries)
            if tries < TA_PROFIT_RETRIES:
                continue  # الربح لم يظهر في السجل بعد، نعيد المحاولة في الدورة التالية
            store.close_trade(t["id"], now_ms, None, None, None)
            store.meta_set(key, None)
            closed.append({**t, "exit_price": None, "r": None, "pnl_usd": None})
            continue
        pnl = round(raw, 2)
        r = round(pnl / t["risk_usd"], 3) if t["risk_usd"] else 0.0
        store.close_trade(t["id"], now_ms, None, r, pnl)
        store.add_balance(pnl)
        store.meta_set(key, None)
        closed.append({**t, "exit_price": None, "r": round(r, 2), "pnl_usd": pnl})
    return closed


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


def ta_open(asset, store, s, g):
    client = ta_client(store)
    if client is None:
        raise BrokerError("أدخل بيانات TickerAll من صفحة الإعدادات أولاً")
    symbol = ta_symbol(store, asset)
    if any(p.get("symbol") == symbol for p in client.positions()):
        return None, f"يوجد مركز مفتوح على {symbol} في المنصة، لن يُفتح مركز آخر"
    spec = client.spec(symbol)
    volume = lots_for_risk(spec, risk_usd(store), abs(s["entry"] - s["stop"]))
    res = client.open_market(symbol, s["side"], volume, s["stop"], s["target"])
    ticket = res.get("ticket")
    if ticket is None:
        raise TickerAllError(502, "NO_TICKET", "لم يُرجع الوسيط رقم الصفقة")
    return record_open(store, asset, key(g), s, "tickerall", qty=volume, order_id=str(ticket)), None


def paper_or_demo_open(asset, store, s, g, mode):
    broker = broker_for(store, mode)
    if broker.name != "paper" and s["side"] == "sell" and not broker.supports_short:
        return None, "تم تجاهل إشارة بيع: السوق المختار لا يدعم البيع على المكشوف"
    qty, order_id = None, None
    if broker.name != "paper":
        ex = broker.place_entry(asset, s["side"], risk_usd(store), s["entry"], s["stop"])
        qty, order_id = ex["qty"], ex["order_id"]
    return record_open(store, asset, key(g), s, mode, qty, order_id), None



# =====================================================================
# محرك التشغيل: الجلسات، السوق، التنبيهات، الدورات
# =====================================================================
def in_session(asset: str, now=None) -> bool:
    now = now or datetime.now(timezone.utc)
    if asset == "BTCUSDT":
        return True
    if asset == "XAUUSD":
        wd = now.weekday()
        if wd >= 5:
            return False
        if wd == 4 and now.hour >= 21:
            return False
        return True
    return False


def next_open_utc(asset, now=None):
    """أقرب فتح للسوق وفق جدول in_session نفسه؛ None إذا كان مفتوحاً أو لا يغلق."""
    now = now or datetime.now(timezone.utc)
    if asset != "XAUUSD" or in_session(asset, now):
        return None
    days = (7 - now.weekday()) % 7 or 7
    return (now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def describe(g: dict) -> str:
    if g.get("rule", "sweep") != "sweep":
        return f"{RULE_AR.get(g['rule'], g['rule'])} | وقف {g.get('atr_stop')}×ATR | هدف {g['target_r']}R | إطار {g['tf']}H"
    return (f"ATR {g['atr_len']} | عمق {g['depth_mult']} | بفر وقف {g['sl_buffer']} | "
            f"هدف {g['target_r']}R | إطار {g['tf']}H | فلتر EMA200 {'نعم' if g['trend'] else 'لا'}")


def fmt(v) -> str:
    return "—" if v is None else f"{v:,.2f}"


TA_CANDLE_TF = {"1m": "M1", "1h": "H1", "1d": "D1"}
INTERVAL_SEC = {"1m": 60, "1h": 3600, "1d": 86400}


def _ts_any(value):
    """يقبل وقتاً ISO أو رقم ميلي ثانية/ثانية."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    return _parse_ts(value)


TA_WINDOW = {"1h": timedelta(days=60), "1d": timedelta(days=400)}
_TA_OK_VARIANT = {}


def _ta_candle_attempts(symbol, tf, interval, count, now):
    """الصيغ المحتملة لطلب الشموع بالترتيب: نطاق زمني صريح أولاً،
    لأن الوسيط يستخدم نافذة 24 ساعة افتراضياً عند تجاهل الوقت."""
    span = TA_WINDOW[interval]
    return [
        ("range", {"symbol": symbol, "timeframe": tf, "count": count,
                   "from": (now - span).isoformat(), "to": now.isoformat()}),
        ("count", {"symbol": symbol, "timeframe": tf, "count": count}),
        ("hours", {"symbol": symbol, "timeframe": tf, "hours": int(span.total_seconds() // 3600)}),
    ]


def ta_history(asset: str, interval: str, count: int, client=None) -> pd.DataFrame:
    """شموع من الوسيط عبر TickerAll. يجرب عدة صيغ للطلب ويسجل أي صيغة نجحت.
    إذا لم ترجع أي صيغة شموعاً يُرفع خطأ واضح (غالباً السوق مغلق أو التاريخ غير محمّل)."""
    client = client or ta_client(store)
    symbol = ta_symbol(store, asset)
    now = datetime.now(timezone.utc)
    items, data, used = [], None, None
    for name, params in _ta_candle_attempts(symbol, TA_CANDLE_TF[interval], interval, count, now):
        data = client._acct("GET", "/candles", params=params)
        items = _items(data, "candles", "data", "items", "bars") or []
        if items:
            used = name
            break
    if used and _TA_OK_VARIANT.get((asset, interval)) != used:
        _TA_OK_VARIANT[(asset, interval)] = used
        log_event("info", f"صيغة طلب الشموع الناجحة: {used} ({interval})", asset)
    if not items:
        raise TickerAllError(502, "NO_CANDLES",
                             f"لا شموع من الوسيط لأي صيغة طلب (غالباً السوق مغلق أو التاريخ غير محمّل): {str(data)[:120]}")
    recs = []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        t = _ts_any(c.get("time") or c.get("timestamp") or c.get("open_time") or c.get("t"))
        if t is None or c.get("open") is None:
            continue
        recs.append({"open_time": t, "open": float(c["open"]), "high": float(c["high"]),
                     "low": float(c["low"]), "close": float(c["close"]),
                     "volume": float(c.get("tick_volume") or c.get("volume") or 0)})
    if not recs:
        raise TickerAllError(502, "NO_CANDLES", f"لم تُفهم الشموع من الرد: {str(data)[:160]}")
    df = pd.DataFrame(recs)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df["close_time"] = df["open_time"] + pd.Timedelta(seconds=INTERVAL_SEC[interval]) - pd.Timedelta(milliseconds=1)
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def load_market(asset: str):
    info = ASSETS[asset]
    h1 = d1 = None
    if ta_creds(store):
        try:
            h1 = ta_history(asset, "1h", HISTORY_H1)
            d1 = ta_history(asset, "1d", HISTORY_D1)
            if len(h1) < 300 or len(d1) < 30:
                raise TickerAllError(502, "SHORT", f"شموع قليلة من الوسيط: H1={len(h1)} D1={len(d1)}")
            h1, d1 = completed_only(h1), completed_only(d1)
        except Exception as exc:
            log_event("warn", f"شموع الوسيط غير متاحة، نستخدم Binance: {str(exc)[:160]}", asset)
            h1 = d1 = None
    if h1 is None:
        h1 = completed_only(fetch_history(info["symbol"], "1h", HISTORY_H1))
        d1 = completed_only(fetch_history(info["symbol"], "1d", HISTORY_D1))
    h1["open_ms"] = [int(t.timestamp() * 1000) for t in h1["open_time"]]
    h1["close_ms"] = [int(t.timestamp() * 1000) for t in h1["close_time"]]
    return h1, d1





def format_open(label, t, g, mode):
    side = "شراء 🟢" if t["side"] == "buy" else "بيع 🔴"
    where = "ورقي" if mode == "paper" else "تجريبي على المنصة"
    return (f"📥 فتح صفقة ({where}) | {label}\n{side} عند {fmt(t['entry'])}\n"
            f"الوقف: {fmt(t['stop'])} | الهدف: {fmt(t['target'])}\n"
            f"مخاطرة: {t['risk_usd']}$ | الاستراتيجية: {describe(g)}\n{DISCLAIMER}")


def format_close(label, t):
    if t.get("pnl_usd") is None:
        result = "الربح غير متاح بعد من سجل الوسيط"
        icon = "ℹ️"
    else:
        icon = "✅" if t["pnl_usd"] > 0 else "❌"
        result = f"{t['r']}R ({t['pnl_usd']:+.2f}$)"
    return (f"{icon} إغلاق صفقة | {label}\nالنتيجة: {result}\n"
            f"سعر الإغلاق: {fmt(t['exit_price'])}\n{DISCLAIMER}")


def format_champion(label, g, result):
    v = result["val"]
    return (f"🧬 اعتماد استراتيجية جديدة | {label}\n{describe(g)}\n"
            f"تحقق خارج العينة: صفقات {v['trades']} | نسبة نجاح {v['win_rate']}% | "
            f"معامل ربح {v['profit_factor']} | أقصى تراجع {v['max_dd_r']}R\n{DISCLAIMER}")


BRAIN_MIN_SAMPLES = 4       # أقل عدد صفقات في السياق قبل أن نحكم عليه
BRAIN_LOSS_EDGE = 2         # يُحظر السياق إذا زادت خسائره على أرباحه بهذا القدر


def brain_refresh(store: Store, asset: str, h1) -> None:
    """يقرأ الصفقات المغلقة للبوت الذكي، ويحظر السياقات التي خسرت مراراً، ويكتب الدروس في lessons.txt."""
    if h1 is None or len(h1) < 120:
        return
    cb, cs, _, _ = _bots.brain_contexts(h1)
    idx = {int(t.timestamp() * 1000): i for i, t in enumerate(h1["open_time"].reset_index(drop=True))}
    stats: dict = {}
    seen_id = int(store.meta_get(f"brain_seen|{asset}") or 0)
    lessons = []
    max_id = seen_id
    for t in store.closed_trades():
        if t["asset"] != asset or t["pnl_usd"] is None:
            continue
        try:
            rule = (json.loads(t["genome"] or "{}") or {}).get("rule")
        except (ValueError, TypeError, AttributeError):
            continue
        if rule != _bots.BRAIN_NAME:
            continue
        i = idx.get(int(t["entry_at"]) - 3600000)
        if i is None:
            continue
        ctx = cb[i] if t["side"] == "buy" else cs[i]
        won = t["pnl_usd"] > 0
        st = stats.setdefault(ctx, {"wins": 0, "losses": 0})
        st["wins" if won else "losses"] += 1
        if t["id"] > seen_id and not won:
            lessons.append(f"خسارة {t['pnl_usd']:+.2f}$ | {t['side']} | {ctx}")
        max_id = max(max_id, t["id"])
    blocked = sorted(c for c, st in stats.items()
                     if st["wins"] + st["losses"] >= BRAIN_MIN_SAMPLES
                     and st["losses"] >= st["wins"] + BRAIN_LOSS_EDGE)
    previous = set(store.meta_get(f"brain_blocked|{asset}") or [])
    for c in blocked:
        if c not in previous:
            st = stats[c]
            lessons.append(f"حظر السياق: {c} | ربح {st['wins']} خسارة {st['losses']}")
    if lessons:
        try:
            path = Path(DB_PATH).resolve().parent / "lessons.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = utc_now()
            with path.open("a", encoding="utf-8") as f:
                for line in lessons:
                    f.write(f"[{stamp}] {asset} | {line}\n")
        except OSError as exc:
            log_event("warn", f"تعذّر كتابة الدروس: {type(exc).__name__}", asset)
    store.meta_set(f"brain_blocked|{asset}", blocked)
    store.meta_set(f"brain_seen|{asset}", max_id)
    _bots.BRAIN_BLOCKED = set(blocked)


def trading_cycle(asset: str, store: Store, send_alerts: bool = True) -> dict:
    label = ASSETS[asset]["label"]
    h1, daily = load_market(asset)
    brain_refresh(store, asset, h1)
    now_ms = int(time.time() * 1000)
    errors, closed = [], []
    extra = {"ta_positions": [], "ta_account": None}
    mode = store.get_mode()

    has_ta_open = any(t["mode"] == "tickerall" for t in store.open_trades(asset))
    client = ta_client(store) if (mode == "tickerall" or has_ta_open) else None
    if client is not None:
        try:
            closed += ta_sync(asset, store, client, now_ms)
            known = {str(t["order_id"]) for t in store.open_trades(asset) if t["mode"] == "tickerall"}
            for p in client.positions():
                extra["ta_positions"].append({**p, "managed": str(p.get("ticket")) in known})
            extra["ta_account"] = client.account_info()
        except Exception as exc:
            errors.append(f"المنصة: {exc}")

    def on_close(t, price):
        if t.get("mode") == "demo" and t.get("qty"):
            broker_for(store, "demo").close_position(t, price)

    paper_closed, close_errors = update_open(store, asset, h1, on_close=on_close)
    closed += paper_closed
    for e in close_errors:
        errors.append(f"تعذّر إغلاق الصفقة على المنصة (تُعاد المحاولة): {e}")
    if send_alerts:
        for e in close_errors:
            send(f"⚠️ {label}: تعذّر إغلاق الصفقة على المنصة، تُعاد المحاولة: {e}")
        for t in closed:
            send(format_close(label, t))

    champ = store.get_champion(asset)
    opened, latest, note = None, None, None
    if champ:
        g = champ["genome"]
        _, sigs, latest = signals_for(g, h1, daily)
        if sigs:
            s = sigs[-1]
            entry_at = int(s["close_ts"].timestamp() * 1000)
            window_ms = (g["tf"] * 60 + 15) * 60 * 1000
            if not in_session(asset):
                note = "السوق مغلق حالياً، لا يُفتح صفقات جديدة"
            elif now_ms - entry_at <= window_ms and can_open(store, asset, entry_at):
                try:
                    if mode == "tickerall":
                        opened, note = ta_open(asset, store, s, g)
                    else:
                        opened, note = paper_or_demo_open(asset, store, s, g, mode)
                except Exception as exc:
                    note = f"تعذّر فتح الصفقة: {exc}"
                if opened and send_alerts:
                    send(format_open(label, opened, g, mode))
                if note and not opened:
                    errors.append(note)
                    if send_alerts:
                        send(f"⚠️ {label}: {note}")
    return {"asset": asset, "price": float(h1["close"].iloc[-1]), "latest": latest,
            "opened": opened, "closed": closed, "has_champion": champ is not None,
            "errors": errors, "note": note, "session_open": in_session(asset), **extra}


def evolve_asset(asset: str, store: Store, generations: int, population: int, send_alerts: bool = True):
    label = ASSETS[asset]["label"]
    if store.meta_get(f"lock|{asset}"):
        return {"changed": False, "label": label, "locked": True}
    h1, daily = load_market(asset)
    before = store.get_champion(asset)
    summary = run_evolution(asset, h1, daily, store, generations, population)
    after = store.get_champion(asset)
    changed = (before or {}).get("genome") != (after or {}).get("genome")
    if after and changed and send_alerts:
        send(format_champion(label, after["genome"], after["result"]))
    return {**summary, "changed": changed, "label": label}

# =====================================================================
# التطبيق: الخادم، الجدولة، واللوحة
# =====================================================================
app = Flask(__name__, static_folder=str(Path(__file__).resolve().parent / "web" / "static"), static_url_path="/static")
store = Store(DB_PATH, CONFIG["start_balance"])
SCAN_DEADLINE_S = 900
scan_state = {"gen": 0, "started": None}
scan_mutex = threading.Lock()


def scan_busy() -> bool:
    with scan_mutex:
        s = scan_state["started"]
        return bool(s and time.time() - s < SCAN_DEADLINE_S)
RUN = {"master": False, "assets": {}}  # يبدأ متوقفاً بعد كل نشر أو إعادة تشغيل
live: dict = {}
meta = {"last_evolve": 0.0, "last_test": None}
activity = deque(maxlen=80)
engine = {"status": "idle", "message": "بانتظار أول فحص", "cycles": 0,
          "last_run": None, "next_run": None}


def log_event(kind, text, asset=None):
    activity.appendleft({"t": int(time.time()), "kind": kind, "asset": asset, "text": str(text)[:300]})
    print(f"[engine] {asset or '-'} {kind}: {text}", flush=True)


def _fmt(v):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


GOLD_MIN_BALANCE = float(os.getenv("GOLD_MIN_BALANCE", "500"))
ZERO_BALANCE_EPS = 1.0


def current_balance() -> float:
    """الرصيد من الوسيط إن توفر، وإلا الرصيد المحلي."""
    snap = store.meta_get("ta_snapshot") or {}
    if snap.get("balance") is not None:
        return float(snap["balance"])
    return store.balance()


def asset_blocked_reason(asset: str, balance: float):
    """قواعد حماية ثابتة: الذهب ممنوع تحت حد الرصيد المحدد."""
    if asset == "XAUUSD" and balance < GOLD_MIN_BALANCE:
        return (f"الذهب محظور حتى يبلغ الرصيد {GOLD_MIN_BALANCE:g}$ "
                f"(الرصيد الحالي {balance:,.2f}$)")
    return None


# أصول يُمنع فيها بوت معين، مع السبب. الذهب مستبعد للبوت الذكي لأن اختباره التاريخي خاسر.
BOT_ASSET_BLOCK = {
    "ai_brain": {"XAUUSD": "البوت الذكي لا يُشغَّل على الذهب: اختباره التاريخي على بيانات حقيقية خاسر، ويعمل على البيتكوين فقط."},
}


def bot_asset_reason(bot: str, asset: str):
    return BOT_ASSET_BLOCK.get(bot, {}).get(asset)


def apply_autostart() -> bool:
    """تشغيل تلقائي عند الإقلاع، مفعّل فقط إذا ضبطت AUTOSTART=1 في Render."""
    if os.getenv("AUTOSTART", "0") != "1":
        return False
    RUN["master"] = True
    for a in (os.getenv("AUTOSTART_ASSETS") or "BTCUSDT").split(","):
        if a.strip():
            RUN["assets"][a.strip()] = True
    log_event("scan", "تشغيل تلقائي عند الإقلاع (AUTOSTART=1)")
    return True


def _scan_body(gen, send_alerts=True):
    try:
        engine.update(status="analyzing", started_at=int(time.time()), current=None,
                      message="بدأ الفحص الآن")
        log_event("scan", "بدء فحص الأصول")
        balance = current_balance()
        if balance <= ZERO_BALANCE_EPS:
            RUN["master"] = False
            log_event("error", f"الرصيد صفر ({balance:,.2f}$): أوقفت التداول تلقائياً")
            alert_once("zero_balance", "تنبيه: تم تصفير الحساب، يرجى إعادة شحنه من البروكر", every=3600)
            engine.update(status="idle", started_at=None, current=None, message="الرصيد صفر، التداول متوقف")
            return
        for asset in store.get_assets():
            label = ASSETS[asset]["label"]
            if not (RUN["master"] and RUN["assets"].get(asset)):
                log_info_once(asset, "متوقف: شغّل التداول لهذا الأصل من لوحة المحرك")
                continue
            blocked = asset_blocked_reason(asset, balance)
            if blocked:
                log_info_once(asset, blocked)
                continue
            engine.update(current=label, message=f"تحميل بيانات {label} وتحليلها...")
            try:
                box = {}

                def _run_cycle(asset=asset, box=box):
                    try:
                        box["res"] = trading_cycle(asset, store, send_alerts=send_alerts)
                    except Exception as exc:
                        box["err"] = exc

                worker = threading.Thread(target=_run_cycle, daemon=True)
                worker.start()
                worker.join(CYCLE_TIMEOUT_S)
                if worker.is_alive():
                    raise TimeoutError(f"تجاوز الفحص المهلة ({CYCLE_TIMEOUT_S} ث)")
                if "err" in box:
                    raise box["err"]
                res = box["res"]
                live[asset] = {"price": res["price"], "latest": res["latest"],
                               "has_champion": res["has_champion"], "errors": res["errors"],
                               "note": res["note"], "updated": int(time.time()),
                               "ta_positions": res["ta_positions"], "ta_account": res["ta_account"]}
                snap_now = res.get("ta_account") or {}
                if snap_now.get("balance") is not None:
                    store.meta_set("ta_snapshot", {"balance": snap_now.get("balance"), "equity": snap_now.get("equity")})
                for t in res.get("closed") or []:
                    log_event("close", f"إغلاق صفقة {t.get('side', '')} · {_fmt(t.get('r'))}R", asset)
                if res.get("opened"):
                    o = res["opened"]
                    log_event("open", f"فتح صفقة {o.get('side', '')} عند {_fmt(o.get('entry'))}", asset)
                for e in res["errors"]:
                    log_event("warn", e, asset)
                if not res.get("opened") and not res["errors"]:
                    if not res["has_champion"]:
                        log_info_once(asset, "لا توجد استراتيجية معتمدة، التداول متوقف لهذا الأصل")
                    elif res.get("note"):
                        log_event("info", res["note"], asset)
                    else:
                        log_event("info", "فُحص السوق: لا توجد إشارة جديدة", asset)
            except Exception as exc:
                traceback.print_exc()
                live[asset] = {"error": str(exc), "updated": int(time.time())}
                log_event("error", f"خطأ في الفحص: {str(exc)[:160]}", asset)
                alert_once(f"err:{asset}", f"⚠️ خطأ متكرر في فحص {label}: {str(exc)[:160]}")
        mem = _rss_mb()
        if mem and mem > 400:
            alert_once("mem", f"⚠️ ذاكرة الخادم وصلت {mem} ميغابايت (الحد المجاني نحو 512)", every=21600)
        engine["cycles"] += 1
        engine["last_run"] = int(time.time())
        engine.update(status="idle", started_at=None, current=None, message="بانتظار الفحص التالي")

    finally:
        with scan_mutex:
            if scan_state["gen"] == gen:
                scan_state["started"] = None


def run_trading(send_alerts=True):
    """يشغّل الفحص في خيط مستقل بمهلة كاملة. إذا تعطّل فحص يتجاوز المهلة يُترك جانباً
    ويبدأ فحص جديد، فلا يبقى المحرك معلقاً إلى الأبد."""
    now = time.time()
    with scan_mutex:
        s = scan_state["started"]
        if s and now - s < SCAN_DEADLINE_S:
            return False
        if s:
            log_event("error", "فحص سابق تجاوز المهلة، تم تجاوزه وبدأ فحص جديد")
        scan_state["gen"] += 1
        my_gen = scan_state["gen"]
        scan_state["started"] = now
    t = threading.Thread(target=_scan_body, args=(my_gen, send_alerts), daemon=True)
    t.start()
    t.join(SCAN_DEADLINE_S)
    if t.is_alive():
        with scan_mutex:
            if scan_state["gen"] == my_gen:
                scan_state["started"] = None
        engine.update(status="idle", started_at=None, current=None, message="تجاوز الفحص المهلة، سيُعاد لاحقاً")
        alert_once("scan_timeout", "⚠️ تجاوز الفحص المهلة المحددة وتم تجاوزه تلقائياً. راجع سجل Render.")
    return True

evolve_lock = threading.Lock()


def run_evolution_job(send_alerts=True):
    """التطوير في قفل منفصل: لا يوقف الفحص الدوري ولا الفحص اليدوي."""
    if not evolve_lock.acquire(blocking=False):
        return False
    try:
        engine["evolving"] = True
        log_event("evolve", "بدأ البحث التطوري عن استراتيجيات أفضل")
        for asset in store.get_assets():
            try:
                res = evolve_asset(asset, store, CONFIG["evolve_generations"], CONFIG["evolve_population"], send_alerts)
                log_event("evolve", "اعتُمدت استراتيجية جديدة" if res.get("changed") else "لم تتغير الاستراتيجية المعتمدة", asset)
            except Exception:
                traceback.print_exc()
                log_event("error", "فشل التطوير لهذا الأصل", asset)
        meta["last_evolve"] = time.time()
        store.meta_set("last_evolve", meta["last_evolve"])
        return True
    finally:
        engine["evolving"] = False
        evolve_lock.release()


_alert_last: dict = {}
_info_last: dict = {}


def alert_once(key, text, every=1800):
    """تنبيه Telegram بحد زمني لكل مفتاح، حتى لا تُغرق القناة بالتكرار."""
    now = time.time()
    if now - _alert_last.get(key, 0) >= every:
        _alert_last[key] = now
        send(text)


def log_info_once(asset, text):
    """يسجل المعلومة فقط عند تغيّرها، لتجنب تكرارها في كل فحص."""
    if _info_last.get(asset) != text:
        _info_last[asset] = text
        log_event("info", text, asset)


def _rss_mb():
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def reconcile_with_broker():
    """يقارن الصفقات المفتوحة محلياً بمراكز الوسيط ويسجل الفروقات دون تعديل تلقائي."""
    if store.get_mode() != "tickerall" or not ta_creds(store):
        return None
    client = ta_client(store)
    if client is None:
        return None
    try:
        broker = client.positions() or []
    except Exception as exc:
        log_event("warn", f"تعذرت المطابقة مع الوسيط: {str(exc)[:120]}")
        return None
    tickets = {str(p.get("ticket") or p.get("id") or "") for p in broker if isinstance(p, dict)}
    if broker and tickets == {""}:
        log_event("warn", "المطابقة متوقفة: رد الوسيط لا يحتوي رقم التذكرة بالحقل المتوقع")
        return None
    local = [t for t in store.open_trades() if t.get("mode") == "tickerall"]
    missing = [t for t in local if str(t.get("order_id") or "") not in tickets]
    for t in missing:
        log_event("warn", f"صفقة مفتوحة محلياً غير موجودة عند الوسيط (تذكرة {t.get('order_id')}), "
                  "ربما أُغلقت بوقف أو هدف", t["asset"])
    if not missing:
        log_event("info", f"المطابقة مع الوسيط: {len(broker)} مركز، لا فروقات")
    return {"broker": len(broker), "local": len(local), "missing_at_broker": len(missing)}


def _keepalive_loop():
    """يبقي جلسة الوسيط حية، ويعيد الاتصال عند الانقطاع."""
    while True:
        time.sleep(240)
        if not ta_creds(store):
            continue
        try:
            ta_client(store).account_info()
            meta["ta_test"] = {"ok": True, "at": int(time.time()), "note": "الجلسة نشطة"}
        except Exception:
            try:
                ta_client(store).connect()
                meta["ta_test"] = {"ok": True, "at": int(time.time()), "note": "أُعيد الاتصال تلقائياً"}
                print("[tickerall] keep-alive: reconnected", flush=True)
            except Exception as exc:
                print(f"[tickerall] keep-alive failed: {type(exc).__name__}", flush=True)


def maybe_daily_summary():
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if now.hour < 21 or store.meta_get("last_summary_day") == today:
        return
    store.meta_set("last_summary_day", today)
    closed = [t for t in store.closed_trades() if str(t.get("closed_utc") or "").startswith(today)]
    pnl = sum((t.get("pnl_usd") or 0) for t in closed)
    send(f"📊 ملخص اليوم {today}\n"
         f"الرصيد المسجل: ${store.balance():,.2f}\n"
         f"صفقات أُغلقت اليوم: {len(closed)} | الربح/الخسارة: ${pnl:,.2f}\n"
         f"صفقات مفتوحة: {len(store.open_trades())}\n{DISCLAIMER}")


def _scheduler_tick():
    try:
        run_trading()
    except Exception:
        traceback.print_exc()
        engine.update(status="idle", current=None, started_at=None,
                      message="خطأ في الفحص، سيُعاد المحاولة في الدورة التالية")
    try:
        maybe_daily_summary()
    except Exception as exc:
        print(f"[summary] {exc}", flush=True)
    engine["next_run"] = int(time.time() + CONFIG["check_every_minutes"] * 60)


def _scheduler():
    while True:
        _scheduler_tick()
        time.sleep(CONFIG["check_every_minutes"] * 60)


_stuck_reported = {"at": None}


def _watchdog_check(now=None):
    """إذا بقي الفحص متصلاً أكثر من STUCK_S يُسجَّل تشخيص كامل لكل الخيوط في سجل Render."""
    now = now or time.time()
    started = engine.get("started_at")
    if engine.get("status") != "analyzing" or not started or now - started < STUCK_S:
        return False
    if _stuck_reported["at"] == started:
        return True
    _stuck_reported["at"] = started
    log_event("error", "المحرك عالق منذ أكثر من 15 دقيقة، تم تسجيل تفاصيل التعطل في السجل")
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    alert_once("stuck", "⚠️ المحرك عالق منذ أكثر من 15 دقيقة. راجع سجل Render لتفاصيل التعطل.")
    return True


def _watchdog():
    while True:
        time.sleep(60)
        try:
            _watchdog_check()
        except Exception:
            traceback.print_exc()


def ta_autoconnect():
    """At startup: connect TickerAll using saved or environment credentials, then apply DEFAULT_MODE."""
    try:
        if not ta_creds(store):
            print("[tickerall] no credentials found; skipped auto-connect", flush=True)
            return
        client = ta_client(store)
        client.connect()
        info = client.account_info()
        if info.get("balance") is not None:
            store.meta_set("ta_snapshot", {"balance": info.get("balance"), "equity": info.get("equity")})
        meta["ta_test"] = {"ok": True, "at": int(time.time()), "note": "اتصال تلقائي عند التشغيل"}
        try:
            resolve_ta_symbols(store, client)
        except Exception as exc:
            print(f"[tickerall] symbol resolve: {type(exc).__name__}", flush=True)
        try:
            reconcile_with_broker()
        except Exception as exc:
            print(f"[reconcile] {exc}", flush=True)
        print(f"[tickerall] auto-connected, balance={info.get('balance')} {info.get('currency')}", flush=True)
        if os.getenv("DEFAULT_MODE") == "tickerall":
            store.set_mode("tickerall")
    except TickerAllError as exc:
        meta["ta_test"] = {"ok": False, "error": str(exc)[:200], "at": int(time.time())}
        print(f"[tickerall] auto-connect failed: {exc}", flush=True)
    except Exception as exc:
        meta["ta_test"] = {"ok": False, "error": "فشل الاتصال التلقائي", "at": int(time.time())}
        print(f"[tickerall] auto-connect error: {type(exc).__name__}", flush=True)


_restored = restore_champions(store)
if _restored:
    print(f"[champions] استُعيدت استراتيجيات من الملفات: {', '.join(_restored)}", flush=True)
meta["last_evolve"] = float(store.meta_get("last_evolve") or 0.0)
def _market_loop():
    """يحدّث أسعار السوق في الخلفية كل 8 ثوان، بعيداً عن طلبات الواجهة."""
    while True:
        try:
            live_market(force=True)
        except Exception as exc:
            print(f"[market] refresh failed: {type(exc).__name__}", flush=True)
        time.sleep(8)

apply_autostart()


def _authorized() -> bool:
    expected = os.getenv("ADMIN_PASSWORD", "")
    given = request.headers.get("X-Admin-Password", "")
    return bool(expected) and hmac.compare_digest(given, expected)


def _deny():
    return jsonify({"error": "كلمة المرور غير صحيحة"}), 401


def _mask(value: str) -> str:
    return "••••" + value[-4:] if value and len(value) > 4 else "••••"


def _settings() -> dict:
    creds = store.get_broker_creds()
    ta = ta_creds(store)
    sess = store.meta_get("tickerall_session")
    return {
        "mode": store.get_mode(),
        "modes": [
            {"key": "paper", "name": "ورقي (Paper)", "available": True,
             "note": "صفقات وهمية محاكاة محلياً، لا يُرسل أي أمر لأي منصة."},
            {"key": "tickerall", "name": "MT5 تجريبي (TickerAll)", "available": ta is not None,
             "note": ("حساب تجريبي على Exness عبر TickerAll. الوقف والهدف على جهة الوسيط." if ta
                      else "أدخل بيانات TickerAll أدناه أولاً.")},
            {"key": "demo", "name": "Binance Testnet", "available": creds is not None,
             "note": ("أموال وهمية. للبيتكوين فقط." if creds else "أدخل مفاتيح Binance أدناه أولاً.")},
            {"key": "live", "name": "حقيقي (Live)", "available": False,
             "note": "غير مفعّل في هذه النسخة."},
        ],
        "keys": {"present": creds is not None,
                 "market": creds["market"] if creds else "spot",
                 "market_label": MARKETS[creds["market"]]["label"] if creds else None,
                 "masked": _mask(creds["api_key"]) if creds else None},
        "tickerall": {
            "present": ta is not None,
            "source": ta.get("source") if ta else None,
            "api_key_masked": _mask(ta["api_key"]) if ta else None,
            "login_masked": ("••••" + ta["login"][-3:]) if ta else None,
            "server": ta["server"] if ta else None,
            "symbols": {a: ta_symbol(store, a) for a in ASSETS},
            "session": sess if sess and sess.get("accountId") else None,
            "last_test": meta.get("ta_test"),
        },
        "markets": [{"key": k, "label": v["label"]} for k, v in MARKETS.items()],
        "last_test": meta["last_test"],
    }


def _bot_board(closed: list) -> dict:
    """لوحة البوتات: ربح كل بوت من صفقاته المغلقة، وأي أصل يعمل عليه الآن، والأصول الممنوعة."""
    board = {k: {"id": k, "name": v, "profit": 0.0, "trades": 0, "running": []} for k, v in BOT_AR.items()}
    for t in closed:
        try:
            rule = (json.loads(t["genome"] or "{}") or {}).get("rule")
        except (ValueError, TypeError, AttributeError):
            rule = None
        if rule in board:
            board[rule]["profit"] += t["pnl_usd"] or 0.0
            board[rule]["trades"] += 1
    for a in store.get_assets():
        rule = ((store.get_champion(a) or {}).get("genome") or {}).get("rule")
        if rule in board and RUN["master"] and RUN["assets"].get(a):
            board[rule]["running"].append(a)
    for b in board.values():
        b["profit"] = round(b["profit"], 2)
        b["blocked"] = {a: r for a, r in BOT_ASSET_BLOCK.get(b["id"], {}).items()}
    bal = current_balance()
    blocked = {a: asset_blocked_reason(a, bal) for a in store.get_assets()}
    return {"bots": list(board.values()),
            "total_profit": round(sum(t["pnl_usd"] or 0.0 for t in closed), 2),
            "blocked": {a: r for a, r in blocked.items() if r}}


def _build_state() -> dict:
    balance = store.balance()
    start = CONFIG["start_balance"]
    source, broker_equity = "paper", None
    if store.get_mode() == "tickerall":
        snap = next((live[a]["ta_account"] for a in store.get_assets()
                     if (live.get(a) or {}).get("ta_account")), None) or store.meta_get("ta_snapshot")
        if snap and snap.get("balance") is not None:
            balance = float(snap["balance"])
            broker_equity = snap.get("equity")
            source = "tickerall"
            if store.meta_get("ta_start") is None:
                store.meta_set("ta_start", balance)
            start = float(store.meta_get("ta_start") or balance)
        else:
            source = "tickerall-pending"
    closed = store.closed_trades()
    equity, running = [], start
    for t in closed:
        running += t["pnl_usd"] or 0
        equity.append({"t": t["closed_utc"], "bal": round(running, 2)})
    wins = [t for t in closed if (t["r"] or 0) > 0]
    gross_win = sum((t["pnl_usd"] or 0) for t in wins)
    gross_loss = -sum((t["pnl_usd"] or 0) for t in closed if (t["r"] or 0) <= 0)
    assets = {}
    for a in store.get_assets():
        assets[a] = {"label": ASSETS[a]["label"], "champion": store.get_champion(a),
                     "leaderboard": store.top_strategies(a, 8), "open": store.open_trades(a),
                     "session_open": in_session(a), "next_open": next_open_utc(a), "live": live.get(a, {})}
    return {
        "version": (os.getenv("RENDER_GIT_COMMIT") or "")[:7] or "محلي",
        "config": {"assets": store.get_assets(), "risk_pct": CONFIG["risk_pct"],
                   "evolve_every_hours": CONFIG["evolve_every_hours"]},
        "settings": _settings(),
        "account": {"balance": balance, "start": start, "source": source, "equity": broker_equity,
                    "return_pct": round((balance / start - 1) * 100, 2),
                    "trades": len(closed),
                    "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
                    "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None},
        "assets": assets,
        "closed": list(reversed(closed[-60:])),
        "equity": equity,
        "evolutions": store.recent_evolutions(12),
        "engine": {**engine, "mode": store.get_mode(), "scan_minutes": CONFIG["check_every_minutes"],
                   "busy": scan_busy(), "now": int(time.time()), "peak_mb": _rss_mb()},
        "activity": list(activity)[:40],
        "bots": [{"id": k, "name": v} for k, v in BOT_AR.items()],
        "board": _bot_board(closed),
        "link": {"mode": store.get_mode(), "source": source,
                 "has_credentials": ta_creds(store) is not None,
                 "test": meta.get("ta_test"),
                 "assets_map": {a: ta_symbol(store, a) for a in store.get_assets()}},
        "trading": {"master": RUN["master"],
                    "assets": {a: bool(RUN["assets"].get(a)) for a in store.get_assets()},
                    "locked": {a: bool(store.meta_get(f"lock|{a}")) for a in store.get_assets()}},
    }


@app.get("/health")
def health():
    return "ok"


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


MARKET_CACHE = {"at": 0.0, "data": None}


def live_market(force: bool = False) -> dict:
    """Live 1-minute prices from Binance public data (PAXG as gold proxy). Cached 8 seconds."""
    now = time.time()
    if not force and MARKET_CACHE["data"] is not None and now - MARKET_CACHE["at"] < 8:
        return MARKET_CACHE["data"]
    out = {}
    for asset, info in ASSETS.items():
        try:
            source = "TickerAll"
            if ta_creds(store):
                try:
                    df = ta_history(asset, "1m", 180)
                except Exception:
                    df = None
            else:
                df = None
            if df is None or len(df) == 0:
                source = "Binance"
                r = requests.get(BINANCE_URL, params={"symbol": info["symbol"], "interval": "1m", "limit": 180},
                                 timeout=TIMEOUT)
                r.raise_for_status()
                rows = r.json()
                closes = [float(x[4]) for x in rows]
                times = [int(x[0]) for x in rows]
            else:
                closes = df["close"].tolist()
                times = [int(t.timestamp() * 1000) for t in df["open_time"]]
            last, first = closes[-1], closes[0]
            out[asset] = {"label": info["label"], "price": last, "change_pct": round((last / first - 1) * 100, 3),
                          "closes": closes, "times": times, "updated": int(now), "source": source,
                          "session_open": in_session(asset),
                          "proxy": source == "Binance" and asset == "XAUUSD"}
        except Exception as exc:
            out[asset] = {"label": info["label"], "error": f"تعذّر جلب السعر: {type(exc).__name__}",
                          "updated": int(now)}
    MARKET_CACHE.update(at=now, data=out)
    return out


@app.get("/api/market")
def market():
    """يقرأ من الذاكرة فقط؛ الجلب من الشبكة يتم في خيط الخلفية، فلا يحجب الطلبات."""
    data = MARKET_CACHE["data"] or {}
    return jsonify({"assets": data,
                    "source": "الوسيط TickerAll، وBinance عند الغياب (الذهب عبر PAXG)",
                    "now": int(time.time()),
                    "age_sec": int(time.time() - MARKET_CACHE["at"]) if MARKET_CACHE["at"] else None})



@app.get("/api/state")
def state():
    return jsonify(_build_state())


@app.post("/api/config")
def set_config():
    if not _authorized():
        return _deny()
    choice = (request.get_json(silent=True) or {}).get("assets")
    valid = {"all": list(ASSETS)}
    valid.update({a: [a] for a in ASSETS})
    if choice not in valid:
        return jsonify({"error": "اختيار أصل غير صالح"}), 400
    store.set_assets(valid[choice])
    return jsonify({"ok": True, "note": "تم حفظ اختيار الأصول"})


@app.post("/api/keys")
def save_keys():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    api_secret = (body.get("api_secret") or "").strip()
    market = body.get("market", "spot")
    if not api_key or not api_secret:
        return jsonify({"error": "أدخل API Key و API Secret"}), 400
    if len(api_key) > 200 or len(api_secret) > 200:
        return jsonify({"error": "القيمة طويلة جداً"}), 400
    if market not in MARKETS:
        return jsonify({"error": "نوع السوق غير صالح"}), 400
    store.set_broker_creds(api_key, api_secret, market)
    meta["last_test"] = None
    return jsonify({"ok": True, "note": "تم حفظ المفاتيح"})


@app.post("/api/keys/clear")
def clear_keys():
    if not _authorized():
        return _deny()
    store.clear_broker_creds()
    if store.get_mode() == "demo":
        store.set_mode("paper")
    meta["last_test"] = None
    return jsonify({"ok": True, "note": "تم حذف المفاتيح وتحويل التنفيذ إلى الورقي"})


@app.post("/api/settings")
def set_settings():
    if not _authorized():
        return _deny()
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode == "paper":
        store.set_mode("paper")
    elif mode == "tickerall":
        if ta_creds(store) is None:
            return jsonify({"error": "أدخل بيانات TickerAll أولاً"}), 400
        store.set_mode("tickerall")
    elif mode == "demo":
        if store.get_broker_creds() is None:
            return jsonify({"error": "أدخل مفاتيح Binance أولاً"}), 400
        store.set_mode("demo")
    else:
        return jsonify({"error": "هذا الوضع غير مفعّل في هذه النسخة"}), 400
    return jsonify({"ok": True, "note": f"تم التبديل إلى: {mode}"})


def _clean_symbol(v, default):
    v = str(v or "").strip()
    return v[:40] if v else default


@app.post("/api/tickerall")
def save_tickerall():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    old = ta_creds(store) or {}
    api_key = (body.get("api_key") or "").strip() or old.get("api_key", "")
    login = str(body.get("login") or "").strip() or old.get("login", "")
    server = (body.get("server") or "").strip() or old.get("server", "")
    password = (body.get("password") or "").strip() or old.get("password", "")
    if not api_key or not login.isdigit() or not server or not password:
        return jsonify({"error": "أدخل API Key ورقم الحساب (أرقام فقط) والسيرفر وكلمة المرور"}), 400
    if len(api_key) > 200 or len(password) > 200 or len(server) > 80:
        return jsonify({"error": "قيمة طويلة جداً"}), 400
    raw = body.get("symbols") if isinstance(body.get("symbols"), dict) else {}
    symbols = {
        "XAUUSD": _clean_symbol(raw.get("XAUUSD"), TA_DEFAULT_SYMBOLS["XAUUSD"]),
        "BTCUSDT": _clean_symbol(raw.get("BTCUSDT"), TA_DEFAULT_SYMBOLS["BTCUSDT"]),
    }
    store.meta_set("tickerall", {"api_key": api_key, "login": login, "server": server,
                                 "password": password, "symbols": symbols})
    store.meta_set("tickerall_session", None)
    meta["ta_test"] = None
    return jsonify({"ok": True, "note": "تم حفظ بيانات TickerAll. اضغط اتصال واختبار للتحقق."})


@app.post("/api/tickerall/test")
def test_tickerall():
    if not _authorized():
        return _deny()
    client = ta_client(store)
    if client is None:
        return jsonify({"error": "أدخل بيانات TickerAll أولاً"}), 400
    try:
        client.connect()
        info = client.account_info()
        syms = client.symbols()
        matches = [x for x in syms if "XAU" in x.upper() or "BTC" in x.upper()][:30]
        checks = {}
        for a in ASSETS:
            try:
                sp = client.spec(ta_symbol(store, a))
                checks[a] = {"ok": True, "volumeMin": sp.get("volumeMin")}
            except TickerAllError as e:
                checks[a] = {"ok": False, "error": str(e)}
        balance = info.get("balance")
        currency = info.get("currency", "")
        note = f"متصل بالحساب التجريبي. الرصيد: {balance} {currency}".strip()
        meta["ta_test"] = {"ok": True, "note": note, "at": int(time.time())}
        return jsonify({"ok": True, "note": note, "matches": matches, "checks": checks})
    except TickerAllError as exc:
        meta["ta_test"] = {"ok": False, "error": str(exc), "at": int(time.time())}
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        meta["ta_test"] = {"ok": False, "error": str(exc)[:200], "at": int(time.time())}
        return jsonify({"error": "تعذّر الاتصال بالوسيط"}), 502


@app.post("/api/tickerall/clear")
def clear_tickerall():
    if not _authorized():
        return _deny()
    if any(t["mode"] == "tickerall" for t in store.open_trades()):
        return jsonify({"error": "أغلق الصفقات المفتوحة على المنصة أولاً"}), 409
    store.meta_set("tickerall", None)
    store.meta_set("tickerall_session", None)
    meta["ta_test"] = None
    if store.get_mode() == "tickerall":
        store.set_mode("paper")
    store.meta_set("ta_start", None)
    return jsonify({"ok": True, "note": "تم حذف بيانات TickerAll"})


@app.post("/api/broker/test")
def broker_test():
    if not _authorized():
        return _deny()
    try:
        res = broker_for(store, "demo").account()
        meta["last_test"] = {"ok": True, "usdt_free": res["usdt_free"], "market": res["market"], "at": int(time.time())}
        return jsonify({"ok": True, "note": f"الاتصال ناجح، رصيد USDT التجريبي: {res['usdt_free']:,.2f}"})
    except BrokerError as exc:
        meta["last_test"] = {"ok": False, "error": str(exc), "at": int(time.time())}
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        meta["last_test"] = {"ok": False, "error": str(exc)[:200], "at": int(time.time())}
        return jsonify({"error": "تعذّر الاتصال بالمنصة التجريبية"}), 502


@app.post("/api/run")
def run_now():
    if not _authorized():
        return _deny()
    busy = scan_busy()
    threading.Thread(target=run_trading, daemon=True).start()
    if busy:
        note = "المحرك مشغول بعملية أخرى، وسيبدأ الفحص فور انتهائها"
    else:
        note = "بدأ الفحص الآن"
    log_event("scan", "طلب فحص يدوي: " + note)
    return jsonify({"ok": True, "note": note})


@app.post("/api/trading/master")
def trading_master():
    if not _authorized():
        return _deny()
    RUN["master"] = bool((request.get_json(silent=True) or {}).get("on"))
    log_event("scan", "تشغيل التداول" if RUN["master"] else "إيقاف التداول")
    return jsonify({"ok": True, "note": "التداول مفعّل" if RUN["master"] else "التداول متوقف"})


@app.post("/api/trading/asset")
def trading_asset():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    asset = body.get("asset")
    if asset not in ASSETS:
        return jsonify({"error": "أصل غير معروف"}), 400
    RUN["assets"][asset] = bool(body.get("on"))
    log_event("scan", "تشغيل هذا الأصل" if RUN["assets"][asset] else "إيقاف هذا الأصل", asset)
    return jsonify({"ok": True, "note": "تم التحديث"})


@app.post("/api/strategy/select")
def strategy_select():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    asset, idx = body.get("asset"), body.get("index")
    if asset not in ASSETS or not isinstance(idx, int):
        return jsonify({"error": "بيانات غير صالحة"}), 400
    board = store.top_strategies(asset, 8)
    if not 0 <= idx < len(board):
        return jsonify({"error": "الاستراتيجية غير موجودة في القائمة"}), 400
    pick = board[idx]
    store.set_champion(asset, {"genome": pick["genome"], "score": pick["score"],
                               "result": pick["result"], "since": utc_now()})
    store.meta_set(f"lock|{asset}", True)
    log_event("evolve", "اعتُمدت استراتيجية من القائمة يدوياً", asset)
    return jsonify({"ok": True, "note": "تم اعتماد الاستراتيجية يدوياً"})


@app.post("/api/strategy/unlock")
def strategy_unlock():
    if not _authorized():
        return _deny()
    asset = (request.get_json(silent=True) or {}).get("asset")
    if asset not in ASSETS:
        return jsonify({"error": "أصل غير معروف"}), 400
    store.meta_set(f"lock|{asset}", None)
    log_event("evolve", "أُلغي الاختيار اليدوي، يعود التطوير لاختيار الاستراتيجية", asset)
    return jsonify({"ok": True, "note": "أُلغي الاختيار اليدوي"})


@app.get("/api/trades.csv")
def trades_csv():
    if not _authorized():
        return _deny()
    import csv
    import io
    cols = ["id", "asset", "side", "entry_at", "entry", "stop", "target", "r", "pnl_usd",
            "status", "exit_at", "exit_price", "closed_utc", "mode", "order_id"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for t in store.closed_trades():
        writer.writerow([t.get(c) for c in cols])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=trades.csv"})


@app.get("/api/candles.csv")
def candles_csv():
    """شموع الساعة المكتملة لأصل واحد، للاختبار الخارجي (backtest). محمية بكلمة مرور الإدارة."""
    if not _authorized():
        return _deny()
    asset = request.args.get("asset", "XAUUSD")
    if asset not in ASSETS:
        return jsonify({"error": "أصل غير صالح"}), 400
    h1, _ = load_market(asset)
    cols = [c for c in ["open_time", "open", "high", "low", "close", "volume"] if c in h1.columns]
    out = h1[cols].copy()
    out["open_time"] = out["open_time"].astype(str)
    return Response(out.to_csv(index=False), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={asset}_H1.csv"})


@app.get("/api/champions/export")
def champions_export():
    if not _authorized():
        return _deny()
    return jsonify({a: store.get_champion(a) for a in store.get_assets() if store.get_champion(a)})


BOT_DEFAULTS = {"atr_stop": 1.5, "atr_len": 14, "depth_mult": 0.2, "sl_buffer": 0.2,
                "target_r": 2.0, "tf": 1, "trend": False}


@app.post("/api/bot/start")
def bot_start():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    asset, bot = body.get("asset"), body.get("bot")
    if asset not in ASSETS or bot not in BOT_FUNCS:
        return jsonify({"error": "أصل أو بوت غير صالح"}), 400
    blocked = asset_blocked_reason(asset, current_balance()) or bot_asset_reason(bot, asset)
    if blocked:
        return jsonify({"error": blocked}), 400
    genome = {**BOT_DEFAULTS, "rule": bot}
    store.set_champion(asset, {"genome": genome, "score": 0.0,
                               "result": {"note": "تشغيل يدوي من قائمة البوتات"}, "since": utc_now()})
    store.meta_set(f"lock|{asset}", True)
    RUN["master"] = True
    RUN["assets"][asset] = True
    log_event("scan", f"بدء البوت: {BOT_AR[bot]}", asset)
    return jsonify({"ok": True, "note": f"بدأ {BOT_AR[bot]} على {ASSETS[asset]['label']}"})


@app.post("/api/bot/stop")
def bot_stop():
    if not _authorized():
        return _deny()
    asset = (request.get_json(silent=True) or {}).get("asset")
    if asset not in ASSETS:
        return jsonify({"error": "أصل غير صالح"}), 400
    RUN["assets"][asset] = False
    log_event("scan", "إيقاف البوت", asset)
    return jsonify({"ok": True, "note": "تم إيقاف البوت على هذا الأصل"})


INDEX_HTML = (Path(__file__).resolve().parent / "web" / "index.html").read_text(encoding="utf-8")


# تشغيل الخيوط الخلفية في آخر الملف، بعد تعريف كل الدوال والمتغيرات التي تستخدمها،
# حتى لا تبدأ قبل اكتمال التحميل فتفشل بـ NameError في أول دورة.
if os.getenv("DISABLE_SCHEDULER") != "1":
    threading.Thread(target=ta_autoconnect, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
    threading.Thread(target=_keepalive_loop, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_market_loop, daemon=True).start()
