"""مركز التحليل الذاتي: ملف واحد يعمل مباشرة على Render.
يشمل: جلب البيانات، الاختبار التاريخي، البحث التطوري، التنفيذ الورقي والتجريبي، الجدولة، واللوحة."""
import hashlib
import hmac
import json
import math
import os
import random
import resource
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
    "assets": ["XAUUSD", "BTCUSDT"],
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


def fetch_history(symbol: str, interval: str, total: int) -> pd.DataFrame:
    chunks, end, remaining = [], None, total
    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end is not None:
            params["endTime"] = end
        r = requests.get(BINANCE_URL, params=params, timeout=TIMEOUT)
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
RULES = ["sweep", "rsi", "macd", "bollinger", "ema_9_21", "ema_50_200", "supertrend",
         "psar", "ao", "heikin", "dual_thrust", "vwap", "london"]
RULE_AR = {
    "sweep": "قمة/قاع الأمس + سحب", "rsi": "RSI ارتداد", "macd": "تقاطع MACD",
    "bollinger": "اختراق بولينجر", "ema_9_21": "EMA 9/21", "ema_50_200": "EMA 50/200",
    "supertrend": "Supertrend", "psar": "Parabolic SAR", "ao": "Awesome Oscillator",
    "heikin": "Heikin-Ashi", "dual_thrust": "Dual Thrust", "vwap": "VWAP ارتداد",
    "london": "اختراق نطاق آسيا/لندن",
}


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


def _rule_masks(rule: str, df: pd.DataFrame):
    close = df["close"]
    n = len(df)
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


def passes(res: dict) -> bool:
    t, v = res["train"], res["val"]
    return (t["trades"] >= MIN_TRAIN_TRADES and t["profit_factor"] >= MIN_PF
            and v["trades"] >= MIN_VAL_TRADES and v["profit_factor"] >= MIN_PF
            and v["max_dd_r"] <= MAX_VAL_DD_R and v["expectancy_r"] > 0)


def score(res: dict) -> float:
    v = res["val"]
    return round(v["expectancy_r"] * math.sqrt(min(v["trades"], 50)), 3)


def run_evolution(asset, h1, daily, store, generations, population, seed=None) -> dict:
    rng = random.Random(seed)
    champ = store.get_champion(asset)
    parents = [champ["genome"]] if champ else []
    seen, accepted, evaluated = set(), [], 0

    for gen in range(generations):
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
            k = key(g)
            if k in seen:
                continue
            seen.add(k)
            res, _ = evaluate(g, h1, daily)
            evaluated += 1
            ok, sc = passes(res), score(res)
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


def can_open(store: Store, asset, entry_at) -> bool:
    if store.balance() <= 0:
        return False
    if store.open_trades(asset):
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
TA_DEFAULT_SYMBOLS = {"XAUUSD": "XAUUSD", "BTCUSDT": "BTCUSD"}
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

    def closed_profit(self, symbol, since_iso) -> float:
        since = _parse_ts(since_iso)
        data = self._acct("GET", "/history", params={"symbol": symbol, "from": since_iso, "limit": 50})
        for item in _items(data, "history", "items", "deals"):
            close = _parse_ts(item.get("closeTime"))
            if close and since and close >= since:
                return float(item.get("profit") or 0) + float(item.get("swap") or 0) \
                    + float(item.get("commission") or 0)
        return 0.0


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
    return (c.get("symbols") or {}).get(asset) or TA_DEFAULT_SYMBOLS[asset]


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
        pnl = round(client.closed_profit(ta_symbol(store, asset), t["opened_utc"]), 2)
        r = round(pnl / t["risk_usd"], 3) if t["risk_usd"] else 0.0
        store.close_trade(t["id"], now_ms, None, r, pnl)
        store.add_balance(pnl)
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
    icon = "✅" if t["pnl_usd"] > 0 else "❌"
    return (f"{icon} إغلاق صفقة | {label}\nالنتيجة: {t['r']}R ({t['pnl_usd']:+.2f}$)\n"
            f"سعر الإغلاق: {fmt(t['exit_price'])}\n{DISCLAIMER}")


def format_champion(label, g, result):
    v = result["val"]
    return (f"🧬 اعتماد استراتيجية جديدة | {label}\n{describe(g)}\n"
            f"تحقق خارج العينة: صفقات {v['trades']} | نسبة نجاح {v['win_rate']}% | "
            f"معامل ربح {v['profit_factor']} | أقصى تراجع {v['max_dd_r']}R\n{DISCLAIMER}")


def trading_cycle(asset: str, store: Store, send_alerts: bool = True) -> dict:
    label = ASSETS[asset]["label"]
    h1, daily = load_market(asset)
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
app = Flask(__name__)
store = Store(DB_PATH, CONFIG["start_balance"])
work_lock = threading.Lock()
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


def run_trading(send_alerts=True):
    with work_lock:
        engine.update(status="analyzing", started_at=int(time.time()), current=None,
                      message="بدأ الفحص الآن")
        log_event("scan", "بدء فحص الأصول")
        for asset in store.get_assets():
            label = ASSETS[asset]["label"]
            engine.update(current=label, message=f"تحميل بيانات {label} وتحليلها...")
            try:
                res = trading_cycle(asset, store, send_alerts=send_alerts)
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
        time.sleep(600)
        if not ta_creds(store):
            continue
        try:
            ta_client(store).account_info()
        except Exception:
            try:
                ta_client(store).connect()
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


def _scheduler():
    while True:
        run_trading()
        try:
            maybe_daily_summary()
        except Exception as exc:
            print(f"[summary] {exc}", flush=True)
        if time.time() - meta["last_evolve"] >= CONFIG["evolve_every_hours"] * 3600 and not evolve_lock.locked():
            threading.Thread(target=run_evolution_job, daemon=True).start()
        engine["next_run"] = int(time.time() + CONFIG["check_every_minutes"] * 60)
        time.sleep(CONFIG["check_every_minutes"] * 60)


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
if os.getenv("DISABLE_SCHEDULER") != "1":
    threading.Thread(target=ta_autoconnect, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
    threading.Thread(target=_keepalive_loop, daemon=True).start()


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
    gross_win = sum(t["pnl_usd"] for t in wins)
    gross_loss = -sum(t["pnl_usd"] for t in closed if (t["r"] or 0) <= 0)
    assets = {}
    for a in store.get_assets():
        assets[a] = {"label": ASSETS[a]["label"], "champion": store.get_champion(a),
                     "leaderboard": store.top_strategies(a, 8), "open": store.open_trades(a),
                     "session_open": in_session(a), "next_open": next_open_utc(a), "live": live.get(a, {})}
    return {
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
                   "busy": work_lock.locked(), "now": int(time.time()), "peak_mb": _rss_mb()},
        "activity": list(activity)[:40],
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
                          "closes": closes, "times": times, "updated": int(now), "source": source}
        except Exception as exc:
            out[asset] = {"label": info["label"], "error": f"تعذّر جلب السعر: {type(exc).__name__}",
                          "updated": int(now)}
    MARKET_CACHE.update(at=now, data=out)
    return out


@app.get("/api/market")
def market():
    return jsonify({"assets": live_market(force=request.args.get("force") == "1"),
                    "source": "الوسيط TickerAll، وBinance عند الغياب (الذهب عبر PAXG)", "now": int(time.time())})


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
    busy = work_lock.locked()
    threading.Thread(target=run_trading, daemon=True).start()
    if busy:
        note = "المحرك مشغول بعملية أخرى، وسيبدأ الفحص فور انتهائها"
    else:
        note = "بدأ الفحص الآن"
    log_event("scan", "طلب فحص يدوي: " + note)
    return jsonify({"ok": True, "note": note})


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


@app.get("/api/champions/export")
def champions_export():
    if not _authorized():
        return _deny()
    return jsonify({a: store.get_champion(a) for a in store.get_assets() if store.get_champion(a)})


@app.post("/api/evolve")
def evolve_now():
    if not _authorized():
        return _deny()
    if evolve_lock.locked():
        return jsonify({"ok": True, "note": "التطوير يعمل حالياً، النتائج ستظهر عند انتهائه"})
    threading.Thread(target=run_evolution_job, daemon=True).start()
    return jsonify({"ok": True, "note": "بدأ التطوير، قد يستغرق عدة دقائق دون إيقاف الفحص"})


INDEX_HTML = r'''
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>مركز التحليل الذاتي</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #04060a;
    --bg2: #070b12;
    --surface: #0b1019;
    --surface2: #0f1620;
    --line: rgba(148, 163, 184, .10);
    --line2: rgba(148, 163, 184, .18);
    --text: #e8edf5;
    --muted: #6b7789;
    --gold: #e7b75a;
    --up: #2fd3a0;
    --down: #f4637a;
    --info: #4aa8ff;
    --mono: "JetBrains Mono", ui-monospace, monospace;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; background: var(--bg); }
  body {
    margin: 0;
    font-family: "IBM Plex Sans Arabic", "Segoe UI", system-ui, sans-serif;
    color: var(--text);
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
  }
  .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted); }
  .up { color: var(--up); }
  .down { color: var(--down); }
  .info { color: var(--info); }
  .gold { color: var(--gold); }

  /* layout */
  .app { display: grid; grid-template-columns: 232px minmax(0, 1fr); min-height: 100vh; overflow-x: hidden; }
  .side { position: sticky; top: 0; height: 100vh; border-left: 1px solid var(--line); background: var(--bg2); padding: 18px 14px; display: flex; flex-direction: column; gap: 22px; }
  .main { min-width: 0; display: flex; flex-direction: column; }
  .topbar { position: sticky; top: 0; z-index: 10; background: rgba(4,6,10,.85); backdrop-filter: blur(10px); border-bottom: 1px solid var(--line); padding: 12px 22px; display: flex; flex-wrap: wrap; align-items: center; gap: 12px; justify-content: space-between; }
  .content { padding: 20px 22px 40px; display: flex; flex-direction: column; gap: 16px; min-width: 0; }
  .panel { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; }
  .panel-h { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 16px 0; }
  .panel-b { padding: 14px 16px 16px; }
  .eyebrow { font-size: 11px; color: var(--muted); letter-spacing: .04em; }
  .title { font-size: 15px; font-weight: 600; }
  .grid-main { display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr) minmax(0, 320px); min-width: 0; }
  .grid-2 { display: grid; gap: 16px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-prices { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-kpi { display: grid; gap: 12px; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .stack { display: grid; gap: 16px; align-content: start; min-width: 0; }
  .panel, .glass { min-width: 0; }

  /* nav */
  .brand { display: flex; align-items: center; gap: 10px; }
  .logo { width: 36px; height: 36px; border-radius: 10px; background: linear-gradient(135deg, var(--gold), var(--up)); color: #04060a; font-weight: 700; display: grid; place-items: center; font-size: 13px; }
  .nav a { display: flex; align-items: center; justify-content: space-between; padding: 9px 12px; border-radius: 10px; color: var(--muted); cursor: pointer; font-weight: 500; border: 1px solid transparent; }
  .nav a:hover { color: var(--text); background: rgba(255,255,255,.03); }
  .nav a.on { color: var(--text); background: var(--surface2); border-color: var(--line2); }
  .nav-count { font-family: var(--mono); font-size: 11px; color: var(--muted); }

  /* controls */
  .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px 14px; border-radius: 10px; border: 1px solid var(--line2); background: var(--surface2); color: var(--text); font: inherit; font-weight: 600; font-size: 13px; cursor: pointer; white-space: nowrap; transition: background .15s, border-color .15s; }
  .btn:hover { background: #151e2b; border-color: rgba(148,163,184,.3); }
  .btn-primary { background: var(--gold); color: #0a0c10; border-color: transparent; }
  .btn-primary:hover { background: #f0c46c; }
  .btn-danger { color: #ffb3c0; background: rgba(244,99,122,.08); border-color: rgba(244,99,122,.25); }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .inp { width: 100%; padding: 9px 11px; border-radius: 10px; border: 1px solid var(--line2); background: #060a10; color: var(--text); font: inherit; font-size: 13px; outline: none; }
  .inp:focus { border-color: rgba(231,183,90,.55); }
  .inp.bad { border-color: var(--down); }
  .label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .seg { display: inline-flex; padding: 3px; gap: 3px; background: #060a10; border: 1px solid var(--line); border-radius: 11px; }
  .seg button { border: 0; background: transparent; color: var(--muted); font: inherit; font-size: 13px; font-weight: 600; padding: 6px 12px; border-radius: 8px; cursor: pointer; }
  .seg button.on { background: var(--surface2); color: var(--text); box-shadow: inset 0 0 0 1px var(--line2); }

  .pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--line2); color: var(--muted); white-space: nowrap; }
  .pill:empty { display: none; }
  .pill.up { color: var(--up); border-color: rgba(47,211,160,.3); background: rgba(47,211,160,.06); }
  .pill.down { color: var(--down); border-color: rgba(244,99,122,.3); background: rgba(244,99,122,.06); }
  .pill.gold { color: var(--gold); border-color: rgba(231,183,90,.3); background: rgba(231,183,90,.06); }
  .pill.info { color: var(--info); border-color: rgba(74,168,255,.3); background: rgba(74,168,255,.06); }
  .dot { width: 7px; height: 7px; border-radius: 999px; background: var(--muted); flex: none; }
  .dot.up { background: var(--up); box-shadow: 0 0 8px var(--up); }
  .dot.down { background: var(--down); }
  .dot.live { background: var(--up); animation: blink 1.6s ease-in-out infinite; }
  @keyframes blink { 50% { opacity: .35; } }

  /* price cards */
  .pcard { padding: 16px; cursor: pointer; transition: border-color .15s; }
  .pcard:hover { border-color: var(--line2); }
  .pcard.sel { border-color: rgba(231,183,90,.4); }
  .pval { font-family: var(--mono); font-size: 26px; font-weight: 500; letter-spacing: -.02em; margin-top: 6px; }
  .pchg { font-family: var(--mono); font-size: 13px; }

  /* kpi */
  .kpi { padding: 14px 16px; }
  .kval { font-family: var(--mono); font-size: 22px; font-weight: 500; margin-top: 4px; }
  .kval small { font-size: 12px; color: var(--muted); }
  .bar { height: 4px; border-radius: 4px; background: rgba(148,163,184,.12); overflow: hidden; margin-top: 10px; }
  .bar > span { display: block; height: 100%; background: var(--up); border-radius: 4px; transition: width .5s; }

  /* tables */
  .tbl { width: 100%; border-collapse: collapse; }
  .tbl th { text-align: right; font-weight: 500; font-size: 12px; color: var(--muted); padding: 9px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  .tbl td { padding: 10px; border-bottom: 1px solid rgba(148,163,184,.06); font-size: 13px; white-space: nowrap; }
  .tbl tbody tr:hover td { background: rgba(255,255,255,.015); }
  .tbl .mono { font-family: var(--mono); }
  .scroll { overflow-x: auto; }
  .empty { padding: 26px; text-align: center; color: var(--muted); font-size: 13px; }

  /* engine radar */
  .radar { position: relative; width: 52px; height: 52px; flex: none; }
  .radar i { position: absolute; inset: 0; border-radius: 50%; border: 1px solid rgba(47,211,160,.55); opacity: 0; }
  .radar b { position: absolute; inset: 37%; border-radius: 50%; background: #3b4657; }
  .radar.on b { background: var(--up); box-shadow: 0 0 12px var(--up); }
  .radar.on i { animation: ripple 1.8s ease-out infinite; }
  .radar.on i:nth-child(2) { animation-delay: .6s; }
  .radar.on i:nth-child(3) { animation-delay: 1.2s; }
  @keyframes ripple { 0% { transform: scale(.35); opacity: .9; } 100% { transform: scale(1.5); opacity: 0; } }
  .feed li { display: flex; gap: 10px; align-items: flex-start; padding: 9px 10px; border-radius: 10px; background: #070b12; border: 1px solid var(--line); font-size: 13px; }
  .feed .tag { font-size: 11px; padding: 2px 8px; border-radius: 6px; background: var(--surface2); color: var(--muted); white-space: nowrap; }
  .feed .tag.open { color: var(--up); } .feed .tag.close { color: var(--gold); }
  .feed .tag.warn, .feed .tag.error { color: var(--down); } .feed .tag.evolve, .feed .tag.scan { color: var(--info); }

  /* brokers */
  .modes { display: grid; gap: 10px; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .mode { display: block; padding: 14px; border-radius: 12px; border: 1px solid var(--line2); background: #070b12; cursor: pointer; }
  .mode.dis { opacity: .4; cursor: not-allowed; }
  .mode.sel { border-color: rgba(231,183,90,.5); }
  .mode .n { font-weight: 600; font-size: 13px; }
  .mode .d { font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.6; }
  .form { display: grid; gap: 14px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  details summary { cursor: pointer; list-style: none; }
  details summary::-webkit-details-marker { display: none; }
  pre.out { margin: 0; white-space: pre-wrap; font-family: var(--mono); font-size: 12px; color: #b8c2d1; background: #060a10; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; min-height: 44px; }

  .chart-box { position: relative; height: 300px; }
  .foot { font-size: 11px; color: var(--muted); line-height: 1.7; padding: 4px 4px 0; }
  .mob-nav { display: none; }
  .hidden { display: none !important; }
  canvas { max-width: 100%; }
  .content > * { width: 100%; min-width: 0; }
  .chart-box { width: 100%; min-width: 0; }
  .main, .topbar { min-width: 0; max-width: 100%; }

  @media (max-width: 1100px) {
    .grid-main { grid-template-columns: minmax(0, 1fr); }
    .grid-kpi { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 860px) {
    .app { grid-template-columns: minmax(0, 1fr); }
    .side { display: none; }
    .grid-2, .grid-prices, .form, .modes, .grid-main { grid-template-columns: minmax(0, 1fr); }
    .content { padding: 14px 14px 96px; }
    .topbar { padding: 10px 14px; }
    .mob-nav { display: flex; position: fixed; bottom: 0; left: 0; right: 0; z-index: 20; background: rgba(7,11,18,.96); border-top: 1px solid var(--line); justify-content: space-around; padding: 8px 6px calc(8px + env(safe-area-inset-bottom)); }
    .mob-nav button { flex: 1; border: 0; background: transparent; color: var(--muted); font: inherit; font-size: 12px; padding: 6px; border-radius: 8px; cursor: pointer; }
    .mob-nav button.on { color: var(--text); background: var(--surface2); }
  }
</style>
</head>
<body>
<div class="app">

  <!-- ============ Sidebar ============ -->
  <aside class="side">
    <div class="brand">
      <div class="logo">MA</div>
      <div>
        <div style="font-weight:700">مركز التحليل</div>
        <div class="eyebrow">Self-improving engine</div>
      </div>
    </div>
    <nav class="nav stack" style="gap:4px">
      <a data-view="terminal" class="on"><span>الرئيسية</span><span class="nav-count" id="navAlerts"></span></a>
      <a data-view="engine"><span>المحرك</span><span class="nav-count" id="navCycles"></span></a>
      <a data-view="brokers"><span>الوسطاء والربط</span></a>
      <a data-view="research"><span>البحث والسجل</span></a>
    </nav>
    <div style="margin-top:auto" class="foot">
      نظام تعليمي يعمل بتنفيذ ورقي أو على حسابات تجريبية فقط. الوضع الحقيقي غير مفعّل. نتائج الاختبار التاريخي لا تضمن أرباحاً مستقبلية.
    </div>
  </aside>

  <main class="main">
    <!-- ============ Top bar ============ -->
    <header class="topbar">
      <div class="flex items-center gap-3" style="display:flex;align-items:center;gap:10px">
        <span id="liveDot" class="dot"></span>
        <span id="liveText" class="muted" style="font-size:13px">جارٍ الاتصال...</span>
        <span id="engPill" class="pill">المحرك: —</span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <input id="pw" type="password" placeholder="كلمة مرور الإدارة" autocomplete="current-password" class="inp" style="width:190px">
        <select id="scope" class="inp" style="width:170px">
          <option value="all">الذهب + البيتكوين</option>
          <option value="XAUUSD">الذهب فقط</option>
          <option value="BTCUSDT">البيتكوين فقط</option>
        </select>
        <button id="btnScope" class="btn">حفظ الأصول</button>
        <button id="btnRun" class="btn">فحص الآن</button>
        <button id="btnEvolve" class="btn btn-primary">تطوير</button>
        <button id="btnExport" class="btn">تصدير الاستراتيجيات</button>
        <button id="btnCsv" class="btn">تصدير الصفقات CSV</button>
      </div>
    </header>
    <p id="msg" class="info" style="margin:0;padding:6px 22px;font-size:12px;min-height:18px"></p>

    <!-- mobile nav -->
    <nav class="mob-nav">
      <button data-view="terminal" class="on">الرئيسية</button>
      <button data-view="engine">المحرك</button>
      <button data-view="brokers">الربط</button>
      <button data-view="research">السجل</button>
    </nav>

    <div class="content">

      <!-- ============ View: terminal ============ -->
      <section data-pane="terminal" class="stack">
        <div id="priceCards" class="grid-prices"></div>

        <div class="grid-main">
          <div class="panel">
            <div class="panel-h">
              <div>
                <div class="title">السعر الحي</div>
                <div class="eyebrow" id="chartMeta">تحديث كل 10 ثوانٍ من Binance</div>
              </div>
              <div style="display:flex;gap:8px;align-items:center">
                <div class="seg" id="chartTabs">
                  <button data-asset="XAUUSD" class="on">الذهب</button>
                  <button data-asset="BTCUSDT">البيتكوين</button>
                </div>
                <button id="btnMarket" class="btn" style="padding:6px 10px">تحديث الآن</button>
              </div>
            </div>
            <div class="panel-b"><div class="chart-box"><canvas id="priceChart"></canvas></div></div>
          </div>

          <div class="stack">
            <div class="panel">
              <div class="panel-b" style="padding-bottom:6px">
                <div class="eyebrow">رصيد الحساب</div>
                <div id="acctBalance" class="mono" style="font-size:30px;margin-top:6px">—</div>
                <div id="acctSub" class="muted" style="font-size:12px;margin-top:4px"></div>
              </div>
              <div class="panel-b" style="display:grid;grid-template-columns:1fr 1fr;gap:14px;border-top:1px solid var(--line)">
                <div><div class="eyebrow">العائد</div><div id="acctReturn" class="mono" style="font-size:16px;margin-top:4px">—</div></div>
                <div><div class="eyebrow">نجاح</div><div id="acctWin" class="mono" style="font-size:16px;margin-top:4px">—</div></div>
                <div><div class="eyebrow">معامل الربح</div><div id="acctPF" class="mono" style="font-size:16px;margin-top:4px">—</div></div>
                <div><div class="eyebrow">صفقات مغلقة</div><div id="acctTrades" class="mono" style="font-size:16px;margin-top:4px">—</div></div>
              </div>
            </div>
            <div class="panel">
              <div class="panel-h"><div class="title">الاستراتيجية المعتمدة</div></div>
              <div id="champBlock" class="panel-b" style="display:grid;gap:10px"></div>
            </div>
          </div>
        </div>

        <div class="grid-2">
          <div class="panel">
            <div class="panel-h">
              <div class="title">الصفقات المفتوحة</div>
              <span id="openCount" class="pill">0</span>
            </div>
            <div class="panel-b scroll"><table class="tbl">
              <thead><tr><th>الأصل</th><th>الاتجاه</th><th>الدخول</th><th>الوقف</th><th>الهدف</th><th>الوضع</th></tr></thead>
              <tbody id="openRows"></tbody>
            </table></div>
            <div id="unmanagedBox" class="panel-b hidden" style="border-top:1px solid var(--line)">
              <div class="eyebrow gold" style="margin-bottom:8px">مراكز على المنصة غير مُدارة من النظام</div>
              <div class="scroll"><table class="tbl">
                <thead><tr><th>الرمز</th><th>الاتجاه</th><th>الحجم</th><th>الربح</th><th>الوقف</th><th>الهدف</th></tr></thead>
                <tbody id="unmanagedRows"></tbody>
              </table></div>
            </div>
          </div>
          <div class="panel">
            <div class="panel-h"><div class="title">الصفقات المغلقة</div><span class="eyebrow">آخر 60</span></div>
            <div class="panel-b scroll" style="max-height:340px;overflow-y:auto"><table class="tbl">
              <thead style="position:sticky;top:0;background:var(--surface)"><tr><th>الأصل</th><th>الاتجاه</th><th>R</th><th>الربح</th><th>الإغلاق</th></tr></thead>
              <tbody id="closedRows"></tbody>
            </table></div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-h"><div><div class="title">منحنى الرصيد</div><div class="eyebrow">الرصيد بعد كل صفقة مغلقة</div></div><span id="equityMeta" class="pill"></span></div>
          <div class="panel-b"><div class="chart-box" style="height:220px"><canvas id="equityChart"></canvas></div>
            <p id="equityEmpty" class="empty hidden">لا توجد صفقات مغلقة بعد.</p></div>
        </div>
      </section>

      <!-- ============ View: engine ============ -->
      <section data-pane="engine" class="hidden stack">
        <div class="panel" style="padding:20px">
          <div style="display:flex;flex-wrap:wrap;gap:20px;align-items:center;justify-content:space-between">
            <div style="display:flex;gap:16px;align-items:center">
              <div id="radar" class="radar"><i></i><i></i><i></i><b></b></div>
              <div>
                <div class="eyebrow">محرك التداول · قواعد تحليل وبحث تطوري وبوابات تحقق (بدون نموذج لغوي)</div>
                <div id="engMsg" style="font-size:17px;font-weight:600;margin-top:6px">—</div>
                <div class="muted" style="font-size:12px;margin-top:4px"><span id="engLast"></span> · <span id="engNext" class="mono"></span></div>
              </div>
            </div>
            <div style="display:flex;gap:26px">
              <div><div class="eyebrow">الفحوصات</div><div id="engCycles" class="mono" style="font-size:20px;margin-top:4px">0</div></div>
              <div><div class="eyebrow">الوضع</div><div id="engMode" style="font-weight:600;margin-top:6px">—</div></div>
              <div><button id="engBtnRun" class="btn btn-primary">فحص الآن</button></div>
            </div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-h"><div class="title">سجل النشاط الحي</div><span class="eyebrow">آخر 40 حدثاً</span></div>
          <div class="panel-b"><ul id="engFeed" class="feed stack" style="gap:8px;max-height:520px;overflow-y:auto"></ul></div>
        </div>
      </section>

      <!-- ============ View: brokers ============ -->
      <section data-pane="brokers" class="hidden stack">
        <div class="panel">
          <div class="panel-h"><div><div class="title">وضع التنفيذ</div><div class="eyebrow">اختر الوضع ثم اضغط تطبيق. الوضع الحقيقي غير مفعّل في هذه النسخة.</div></div></div>
          <div class="panel-b" style="display:grid;gap:14px">
            <div id="modes" class="modes"></div>
            <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
              <button id="btnSetMode" class="btn btn-primary">تطبيق الوضع المختار</button>
              <span id="modeOut" class="muted" style="font-size:12px"></span>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-h">
            <div><div class="title">TickerAll · MT5 تجريبي (Exness)</div><div class="eyebrow">الوقف والهدف يُحملان على جهة الوسيط. الحسابات الحقيقية مرفوضة.</div></div>
            <span id="taBadge" class="pill">غير مرتبط</span>
          </div>
          <div class="panel-b" style="display:grid;gap:16px">
            <div class="form">
              <label><span class="label">API Key</span><input id="taKey" type="text" autocomplete="off" spellcheck="false" class="inp" placeholder="cf_api_…"></label>
              <label><span class="label">رقم الحساب التجريبي</span><input id="taLogin" type="text" inputmode="numeric" autocomplete="off" class="inp"></label>
              <label><span class="label">السيرفر</span><input id="taServer" type="text" autocomplete="off" class="inp" placeholder="Exness-MT5Trial16"></label>
              <label><span class="label">كلمة مرور الحساب التجريبي</span><input id="taPass" type="password" autocomplete="new-password" class="inp"></label>
              <label><span class="label">رمز الذهب لدى الوسيط</span><input id="taSymGold" type="text" class="inp" value="XAUUSD"></label>
              <label><span class="label">رمز البيتكوين لدى الوسيط</span><input id="taSymBtc" type="text" class="inp" value="BTCUSD"></label>
            </div>
            <div class="eyebrow">الحقول أعلاه لحساب المنصة. كلمة مرور الإدارة في أعلى الصفحة حقل منفصل يحمي الحفظ.</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <button id="btnSaveTa" class="btn btn-primary">حفظ</button>
              <button id="btnTestTa" class="btn">اتصال واختبار الرموز</button>
              <button id="btnClearTa" class="btn btn-danger">حذف</button>
            </div>
            <pre id="taOut" class="out"></pre>
          </div>
        </div>

        <details class="panel" style="padding:16px">
          <summary style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <span class="title">Binance Testnet <span class="muted" style="font-weight:400;font-size:12px">· للبيتكوين، أموال وهمية</span></span>
            <span id="keyStatus" class="muted" style="font-size:12px"></span>
          </summary>
          <div class="form" style="margin-top:16px;grid-template-columns:repeat(4,minmax(0,1fr))">
            <label><span class="label">نوع السوق</span><select id="market" class="inp"></select></label>
            <label><span class="label">API Key</span><input id="apiKey" type="text" autocomplete="off" spellcheck="false" class="inp"></label>
            <label><span class="label">API Secret</span><input id="apiSecret" type="password" autocomplete="off" class="inp"></label>
            <div style="display:flex;gap:8px;align-items:flex-end">
              <button id="btnSaveKeys" class="btn btn-primary" style="flex:1">حفظ</button>
              <button id="btnTest" class="btn" style="flex:1">اختبار</button>
              <button id="btnClearKeys" class="btn btn-danger">حذف</button>
            </div>
          </div>
          <p id="testOut" class="muted" style="font-size:12px;margin:12px 0 0"></p>
        </details>
      </section>

      <!-- ============ View: research ============ -->
      <section data-pane="research" class="hidden grid-main" style="grid-template-columns:minmax(0,1fr) minmax(0,340px)">
        <div class="panel">
          <div class="panel-h"><div class="title">لوحة المتصدرين</div><span class="eyebrow">استراتيجيات اجتازت أو لم تجتز</span></div>
          <div class="panel-b scroll"><table class="tbl">
            <thead><tr><th>الأصل</th><th>الاستراتيجية</th><th>نجاح%</th><th>PF</th><th>تراجع R</th><th>الدرجة</th></tr></thead>
            <tbody id="boardRows"></tbody>
          </table></div>
        </div>
        <div class="panel">
          <div class="panel-h"><div class="title">سجل التطوير</div></div>
          <div class="panel-b"><ul id="evoLog" class="stack" style="gap:8px;max-height:520px;overflow-y:auto"></ul></div>
        </div>
      </section>

    </div>
  </main>
</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, d = 2) => (v == null || isNaN(v)) ? "—" : Number(v).toLocaleString("en-US", {maximumFractionDigits: d, minimumFractionDigits: d});
const dt = (s) => s ? String(s).slice(0, 16).replace("T", " ") : "—";
const hm = (ms) => new Date(ms).toLocaleTimeString("ar-SA", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const sgn = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const sideTxt = (s) => (String(s).toLowerCase() === "buy" ? '<span class="up">شراء</span>' : '<span class="down">بيع</span>');
const MODE_AR = { paper: "ورقي", tickerall: "MT5 تجريبي", demo: "Binance Testnet" };
const KIND_TAG = { open: "open", close: "close", warn: "warn", error: "error", evolve: "evolve", scan: "scan", info: "" };
const RULE_AR = { sweep: "قمة/قاع الأمس + سحب", rsi: "RSI ارتداد", macd: "تقاطع MACD", bollinger: "اختراق بولينجر",
  ema_9_21: "EMA 9/21", ema_50_200: "EMA 50/200", supertrend: "Supertrend", psar: "Parabolic SAR",
  ao: "Awesome Oscillator", heikin: "Heikin-Ashi", dual_thrust: "Dual Thrust", vwap: "VWAP ارتداد", london: "اختراق نطاق آسيا/لندن" };
const describe = (g) => {
  if (!g) return "";
  if (g.rule && g.rule !== "sweep") return `${RULE_AR[g.rule] || g.rule} · وقف ${g.atr_stop}×ATR · ${g.target_r}R · ${g.tf}H`;
  return `ATR ${g.atr_len} · عمق ${g.depth_mult} · بفر ${g.sl_buffer} · ${g.target_r}R · ${g.tf}H · EMA200 ${g.trend ? "✔" : "✘"}`;
};

// ---------- admin password: remembered in this browser ----------
const PW_KEY = "ma_admin_pw";
try { $("pw").value = localStorage.getItem(PW_KEY) || ""; } catch (e) {}
$("pw").addEventListener("input", () => { try { localStorage.setItem(PW_KEY, $("pw").value); } catch (e) {} });

// ---------- navigation ----------
function showView(name) {
  document.querySelectorAll("[data-view]").forEach(b => b.classList.toggle("on", b.dataset.view === name));
  document.querySelectorAll("[data-pane]").forEach(p => p.classList.toggle("hidden", p.dataset.pane !== name));
  try { localStorage.setItem("ma_view", name); } catch (e) {}
  if (name === "terminal") { setTimeout(() => { priceChart && priceChart.resize(); eqChart && eqChart.resize(); }, 50); }
}
document.querySelectorAll("[data-view]").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
showView((() => { try { return localStorage.getItem("ma_view") || "terminal"; } catch (e) { return "terminal"; } })());

// ---------- state ----------
let state = null;
let market = null;
let chartAsset = "XAUUSD";
let priceChart = null;
let eqChart = null;
let nextRunAt = null;
let pickedMode = null;

async function loadState() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    state = await r.json();
    renderState(state);
    $("liveDot").className = "dot live";
    $("liveText").textContent = "متصل · تحديث الحالة " + new Date().toLocaleTimeString("ar-SA");
  } catch (e) {
    $("liveDot").className = "dot down";
    $("liveText").textContent = "انقطع الاتصال بالخادم، يُعاد المحاولة...";
  }
}

async function loadMarket(force = false) {
  try {
    const r = await fetch("/api/market" + (force ? "?force=1" : ""), { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    market = await r.json();
    renderPrices();
    drawPrice();
    $("chartMeta").textContent = "آخر تحديث " + hm(market.now * 1000) + " · المصدر: " + market.source;
  } catch (e) {
    $("chartMeta").textContent = "تعذّر تحديث الأسعار حالياً";
  }
}

function msg(t) { $("msg").textContent = t; }

// ---------- render: prices ----------
function fmtOpen(iso) {
  try { return new Date(iso).toLocaleString("ar-SA", { timeZone: "Asia/Riyadh", weekday: "long", hour: "2-digit", minute: "2-digit" }); }
  catch (e) { return iso; }
}

function renderPrices() {
  if (!market) return;
  const assets = market.assets || {};
  const sess = (state && state.assets) || {};
  $("priceCards").innerHTML = Object.entries(assets).map(([k, a]) => {
    const open = sess[k] ? sess[k].session_open : null;
    const pill = open === null ? "" : `<span class="pill ${open ? "up" : "down"}"><span class="dot ${open ? "up" : "down"}"></span>${open ? "السوق مفتوح" : "السوق مغلق"}</span>`;
    const body = a.error
      ? `<div class="down" style="margin-top:10px;font-size:13px">${esc(a.error)}</div>`
      : `<div class="pval">${num(a.price)}</div>
         <div class="pchg ${sgn(a.change_pct)}" style="margin-top:4px">${a.change_pct > 0 ? "+" : ""}${num(a.change_pct, 3)}% <span class="muted">· آخر 3 ساعات</span></div>`;
    const opensTxt = !open && sess[k] && sess[k].next_open ? `<div class="muted" style="font-size:12px;margin-top:6px">يفتح ${fmtOpen(sess[k].next_open)} (بتوقيت الرياض)</div>` : "";
    return `<div class="panel pcard ${chartAsset === k ? "sel" : ""}" data-pick="${k}">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="title">${esc(a.label)}</span>${pill}
      </div>${body}${opensTxt}
    </div>`;
  }).join("");
  document.querySelectorAll("[data-pick]").forEach(el => el.onclick = () => setChartAsset(el.dataset.pick));
}

function setChartAsset(k) {
  chartAsset = k;
  document.querySelectorAll("#chartTabs button").forEach(b => b.classList.toggle("on", b.dataset.asset === k));
  renderPrices();
  drawPrice();
}
document.querySelectorAll("#chartTabs button").forEach(b => b.onclick = () => setChartAsset(b.dataset.asset));

// ---------- render: price chart ----------
function drawPrice() {
  const a = market && market.assets && market.assets[chartAsset];
  if (!a || a.error || !a.closes) return;
  const labels = a.times.map(hm);
  const up = a.change_pct >= 0;
  const color = up ? "#2fd3a0" : "#f4637a";
  if (!priceChart) {
    const ctx = $("priceChart").getContext("2d");
    const grad = ctx.createLinearGradient(0, 0, 0, 300);
    grad.addColorStop(0, "rgba(47,211,160,.22)");
    grad.addColorStop(1, "rgba(47,211,160,0)");
    priceChart = new Chart($("priceChart"), {
      type: "line",
      data: { labels, datasets: [{ data: a.closes, borderColor: color, backgroundColor: grad, fill: true,
        borderWidth: 1.6, pointRadius: 0, tension: 0.25 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: { duration: 400 },
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false },
          tooltip: { backgroundColor: "#070b12", borderColor: "#1f2a3a", borderWidth: 1, titleColor: "#e8edf5",
                     bodyColor: "#e8edf5", displayColors: false, callbacks: { label: c => num(c.parsed.y) } } },
        scales: { x: { ticks: { color: "#6b7789", maxTicksLimit: 6, font: { family: "JetBrains Mono" } }, grid: { color: "rgba(148,163,184,.05)" } },
                  y: { position: "right", ticks: { color: "#6b7789", font: { family: "JetBrains Mono" } }, grid: { color: "rgba(148,163,184,.07)" } } } },
    });
  } else {
    priceChart.data.labels = labels;
    priceChart.data.datasets[0].data = a.closes;
    priceChart.data.datasets[0].borderColor = color;
    priceChart.update();
  }
}

// ---------- render: state ----------
function renderState(s) {
  renderEngine(s.engine, s.activity);
  renderAccount(s.account);
  renderChampion(s.assets, s.account);
  renderOpen(s);
  renderClosed(s.closed);
  drawEquity(s.equity, s.account.start);
  renderBoard(s.assets);
  renderEvolutions(s.evolutions);
  renderSettings(s.settings);
  renderPrices();
  $("openCount").textContent = Object.values(s.assets).reduce((n, v) => n + (v.open || []).length, 0);
}

function renderAccount(a) {
  $("acctBalance").textContent = a.source === "tickerall-pending" ? "—" : "$" + num(a.balance);
  $("acctSub").textContent = a.source === "tickerall" ? "رصيد الحساب التجريبي على الوسيط · مباشر"
    : a.source === "tickerall-pending" ? "بانتظار أول قراءة من الوسيط"
    : "ورقي · البداية $" + num(a.start, 0);
  $("acctReturn").innerHTML = `<span class="${sgn(a.return_pct)}">${num(a.return_pct)}%</span>`;
  $("acctWin").textContent = num(a.win_rate, 1) + "%";
  $("acctPF").textContent = num(a.profit_factor);
  $("acctTrades").textContent = a.trades;
}

function renderChampion(assets, acct) {
  const blocks = Object.entries(assets).map(([k, v]) => {
    const c = v.champion;
    if (!c) return `<div style="border:1px solid rgba(244,99,122,.3);border-radius:10px;padding:10px 12px;font-size:12px;color:#ffb3c0;background:rgba(244,99,122,.06)">${esc(v.label)}: لا توجد استراتيجية معتمدة، التداول متوقف</div>`;
    const t = c.result.val;
    return `<div style="border:1px solid var(--line);border-radius:10px;padding:10px;background:#070b12">
      <div class="title" style="font-size:13px">${esc(v.label)}</div>
      <div class="mono muted" style="font-size:12px;margin-top:6px">${esc(describe(c.genome))}</div>
      <div class="muted" style="font-size:12px;margin-top:6px">تحقق: نجاح ${num(t.win_rate, 1)}% · PF ${num(t.profit_factor)} · تراجع ${num(t.max_dd_r)}R · ${t.trades} صفقة</div>
      <div class="muted" style="font-size:11px;margin-top:4px">منذ ${dt(c.since)}</div></div>`;
  });
  $("champBlock").innerHTML = blocks.join("") || '<div class="empty">لا توجد أصول مفعّلة</div>';
}

function renderOpen(s) {
  const rows = Object.values(s.assets).flatMap(v => v.open || []);
  $("openRows").innerHTML = rows.length ? rows.map(t => `<tr>
      <td>${esc(t.asset)}</td><td>${sideTxt(t.side)}</td>
      <td class="mono">${num(t.entry)}</td><td class="mono">${num(t.stop)}</td><td class="mono">${num(t.target)}</td>
      <td><span class="pill">${MODE_AR[t.mode] || esc(t.mode)}</span></td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">لا توجد صفقات مفتوحة</td></tr>`;
  const unmanaged = Object.values(s.assets).flatMap(v => ((v.live && v.live.ta_positions) || []).filter(p => !p.managed));
  $("unmanagedBox").classList.toggle("hidden", unmanaged.length === 0);
  $("unmanagedRows").innerHTML = unmanaged.map(p => `<tr><td>${esc(p.symbol)}</td><td>${sideTxt(p.side)}</td>
    <td class="mono">${num(p.volume)}</td><td class="mono ${sgn(p.profit)}">${num(p.profit)}</td>
    <td class="mono">${num(p.stopLoss)}</td><td class="mono">${num(p.takeProfit)}</td></tr>`).join("");
}

function renderClosed(closed) {
  $("closedRows").innerHTML = closed.length ? closed.map(t => `<tr>
      <td>${esc(t.asset)}</td><td>${sideTxt(t.side)}</td>
      <td class="mono ${sgn(t.r)}">${num(t.r)}R</td>
      <td class="mono ${sgn(t.pnl_usd)}">${t.pnl_usd > 0 ? "+" : ""}${num(t.pnl_usd)}$</td>
      <td class="muted mono">${dt(t.closed_utc)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">لا توجد صفقات مغلقة بعد</td></tr>`;
}

function drawEquity(points, start) {
  const empty = points.length === 0;
  $("equityEmpty").classList.toggle("hidden", !empty);
  $("equityMeta").textContent = empty ? "" : `${points.length} صفقة مغلقة`;
  if (empty) { if (eqChart) { eqChart.destroy(); eqChart = null; } return; }
  const labels = ["البداية", ...points.map(p => dt(p.t))];
  const data = [start, ...points.map(p => p.bal)];
  if (!eqChart) {
    eqChart = new Chart($("equityChart"), {
      type: "line",
      data: { labels, datasets: [{ data, borderColor: "#e7b75a", borderWidth: 1.8, pointRadius: 0, tension: 0.25, fill: false }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
        scales: { x: { ticks: { color: "#6b7789", maxTicksLimit: 8 }, grid: { color: "rgba(148,163,184,.05)" } },
                  y: { position: "right", ticks: { color: "#6b7789", callback: v => "$" + num(v, 0) }, grid: { color: "rgba(148,163,184,.07)" } } } },
    });
  } else {
    eqChart.data.labels = labels;
    eqChart.data.datasets[0].data = data;
    eqChart.update();
  }
}

// ---------- engine ----------
function renderEngine(e, acts) {
  if (!e) return;
  const running = e.status === "analyzing";
  $("radar").classList.toggle("on", running);
  $("engMsg").textContent = e.message;
  if (running) {
    const secs = e.started_at ? Math.max(0, e.now - e.started_at) : 0;
    $("engLast").textContent = `فحص جارٍ منذ ${secs} ث${e.current ? " · " + e.current : ""}`;
  } else {
    $("engLast").textContent = e.last_run ? "آخر فحص مكتمل " + hm(e.last_run * 1000) : "لم يكتمل أي فحص بعد";
  }
  if (e.evolving) $("engLast").textContent += " · التطوير جارٍ";
  $("engCycles").textContent = e.cycles;
  $("engMode").textContent = MODE_AR[e.mode] || e.mode;
  $("navCycles").textContent = e.cycles;
  $("engPill").textContent = "";
  const pill = $("engPill");
  pill.textContent = running ? "المحرك: يحلل الآن" : "المحرك: جاهز";
  pill.className = "pill " + (running ? "info" : "up");
  nextRunAt = e.next_run ? e.next_run * 1000 : null;
  $("engFeed").innerHTML = acts.length ? acts.map(a => `<li>
      <span class="mono muted" style="font-size:12px;white-space:nowrap">${hm(a.t * 1000)}</span>
      <span class="tag ${KIND_TAG[a.kind] || ""}">${esc(a.asset || "النظام")}</span>
      <span style="color:#cbd5e1">${esc(a.text)}</span></li>`).join("")
    : `<li class="muted">لا توجد أحداث بعد</li>`;
}
function tickCountdown() {
  if (!nextRunAt) { $("engNext").textContent = ""; return; }
  const left = Math.max(0, Math.round((nextRunAt - Date.now()) / 1000));
  $("engNext").textContent = `الفحص التالي خلال ${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
}
setInterval(tickCountdown, 1000);

// ---------- research ----------
function renderBoard(assets) {
  const board = Object.entries(assets).flatMap(([k, v]) => v.leaderboard.map(x => ({...x, asset: v.label})));
  $("boardRows").innerHTML = board.length ? board.map(x => `<tr>
      <td>${esc(x.asset)}</td><td class="mono muted" style="font-size:12px">${esc(describe(x.genome))}</td>
      <td class="mono">${num(x.result.val.win_rate, 1)}</td><td class="mono">${num(x.result.val.profit_factor)}</td>
      <td class="mono">${num(x.result.val.max_dd_r)}</td><td class="mono gold">${num(x.score, 3)}</td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">لم تُعتمد أي استراتيجية بعد. اضغط "تطوير".</td></tr>`;
}
function renderEvolutions(list) {
  $("evoLog").innerHTML = list.length ? list.map(e => `<li style="padding:10px;border-radius:10px;background:#070b12;border:1px solid var(--line)">
      <div style="display:flex;justify-content:space-between;font-size:12px" class="muted"><span>${esc(e.asset)}</span><span class="mono">${dt(e.created_utc)}</span></div>
      <div style="margin-top:6px">جُرّب ${e.evaluated} · نجح ${e.accepted}</div>
      <div class="muted" style="font-size:12px;margin-top:3px">${esc(e.note)}</div></li>`).join("")
    : `<li class="muted">لا يوجد سجل بعد</li>`;
}

// ---------- brokers ----------
function renderSettings(st) {
  if (!st) return;
  const cur = st.mode;
  $("modes").innerHTML = st.modes.map(m => `<label class="mode ${m.available ? "" : "dis"} ${(pickedMode || cur) === m.key ? "sel" : ""}">
      <div style="display:flex;align-items:center;gap:8px">
        <input type="radio" name="mode" value="${esc(m.key)}" ${m.available ? "" : "disabled"} ${(pickedMode || cur) === m.key ? "checked" : ""} style="accent-color:#e7b75a">
        <span class="n">${esc(m.name)}</span>${cur === m.key ? '<span class="pill up" style="margin-inline-start:auto">مفعّل</span>' : ""}
      </div>
      <div class="d">${esc(m.note)}</div></label>`).join("");
  document.querySelectorAll('input[name="mode"]').forEach(el => el.onchange = () => { pickedMode = el.value; renderSettings(state.settings); });

  $("keyStatus").textContent = st.keys.present ? "🔑 " + st.keys.masked : "";
  if (!$("market").options.length) $("market").innerHTML = st.markets.map(m => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
  if (st.last_test) $("testOut").textContent = st.last_test.ok ? `✔ آخر اختبار ناجح: ${num(st.last_test.usdt_free)} USDT` : `✘ آخر اختبار فشل: ${st.last_test.error}`;

  const ta = st.tickerall;
  $("taBadge").textContent = ta.present ? (ta.session ? "متصل ●" : "محفوظ · غير متصل") : "غير مرتبط";
  $("taBadge").className = "pill " + (ta.present ? (ta.session ? "up" : "gold") : "");
  if (!$("taSymGold").dataset.touched) $("taSymGold").value = ta.symbols.XAUUSD;
  if (!$("taSymBtc").dataset.touched) $("taSymBtc").value = ta.symbols.BTCUSDT;
  if (!$("taServer").value && ta.server) $("taServer").value = ta.server;
  if (!$("taOut").dataset.busy) $("taOut").textContent = ta.present
    ? `مفتاح: ${ta.api_key_masked} · حساب: ${ta.login_masked} · سيرفر: ${ta.server}` +
      (ta.session ? `\nالجلسة نشطة حتى ${dt(ta.session.expiresAt)}` : "\nلا توجد جلسة نشطة، اضغط اتصال واختبار.") +
      (ta.last_test ? `\n${ta.last_test.ok ? "✔ " + (ta.last_test.note || "متصل") : "✘ " + ta.last_test.error}` : "")
    : "لم تُحفظ بيانات TickerAll بعد.";
}

// ---------- requests ----------
async function postJson(url, body) {
  const pwd = $("pw").value.trim();
  if (!pwd) {
    $("pw").classList.add("bad");
    $("pw").focus();
    return { ok: false, data: { error: "أدخل كلمة مرور الإدارة في أعلى الصفحة أولاً" }, status: 0 };
  }
  $("pw").classList.remove("bad");
  const r = await fetch(url, { method: "POST",
    headers: { "Content-Type": "application/json", "X-Admin-Password": pwd },
    body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) {
    $("pw").classList.add("bad");
    return { ok: false, data: { error: "كلمة مرور الإدارة غير صحيحة (هذه ليست كلمة مرور الحساب التجريبي)" }, status: 401 };
  }
  return { ok: r.ok, data: j, status: r.status };
}

function report(res, el) {
  const text = res.ok ? (res.data.note || "تم ✔") : "✘ " + (res.data.error || ("خطأ " + res.status));
  if (el) { el.textContent = text; el.dataset.busy = ""; } else msg(text);
  setTimeout(loadState, 600);
}

async function runScan(btn) {
  if (btn) btn.disabled = true;
  const res = await postJson("/api/run");
  report(res);
  if (btn) setTimeout(() => btn.disabled = false, 1500);
  loadMarket(true);
}
$("btnRun").onclick = () => runScan($("btnRun"));
$("engBtnRun").onclick = () => runScan($("engBtnRun"));
$("btnScope").onclick = async () => report(await postJson("/api/config", { assets: $("scope").value }));
$("btnEvolve").onclick = async () => report(await postJson("/api/evolve"));
$("btnExport").onclick = async () => {
  const pwd = $("pw").value.trim();
  if (!pwd) { $("pw").classList.add("bad"); $("pw").focus(); return msg("أدخل كلمة مرور الإدارة أولاً"); }
  const r = await fetch("/api/champions/export", { headers: { "X-Admin-Password": pwd } });
  if (!r.ok) return msg("تعذر التصدير (تحقق من كلمة المرور)");
  const txt = JSON.stringify(await r.json(), null, 2);
  try { await navigator.clipboard.writeText(txt); msg("نُسخت الاستراتيجيات. الصقها في الملف seed/champions.json داخل GitHub ثم احفظ."); }
  catch (e) { msg("تعذر النسخ التلقائي، أعد المحاولة من متصفح آخر."); }
};
$("btnCsv").onclick = async () => {
  const pwd = $("pw").value.trim();
  if (!pwd) { $("pw").classList.add("bad"); $("pw").focus(); return msg("أدخل كلمة مرور الإدارة أولاً"); }
  const r = await fetch("/api/trades.csv", { headers: { "X-Admin-Password": pwd } });
  if (!r.ok) return msg("تعذر التصدير (تحقق من كلمة المرور)");
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a"); a.href = url; a.download = "trades.csv"; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("btnMarket").onclick = () => loadMarket(true);

$("btnSetMode").onclick = async () => {
  const sel = document.querySelector('input[name="mode"]:checked');
  if (!sel) return report({ ok: false, data: { error: "اختر وضعاً أولاً" } }, $("modeOut"));
  pickedMode = null;
  report(await postJson("/api/settings", { mode: sel.value }), $("modeOut"));
};

$("btnSaveTa").onclick = async () => {
  $("taOut").dataset.busy = "1"; $("taOut").textContent = "جارٍ الحفظ...";
  const res = await postJson("/api/tickerall", {
    api_key: $("taKey").value, login: $("taLogin").value, server: $("taServer").value,
    password: $("taPass").value,
    symbols: { XAUUSD: $("taSymGold").value, BTCUSDT: $("taSymBtc").value } });
  if (res.ok) { $("taKey").value = ""; $("taPass").value = ""; }
  report(res, $("taOut"));
};
$("btnTestTa").onclick = async () => {
  $("taOut").dataset.busy = "1"; $("taOut").textContent = "جارٍ الاتصال بالوسيط...";
  const res = await postJson("/api/tickerall/test");
  if (!res.ok) return report(res, $("taOut"));
  const m = (res.data.matches || []).join("، ") || "لم يُعثر على رموز ذهب أو بيتكوين";
  const lines = Object.entries(res.data.checks || {}).map(([a, v]) => `${a}: ${v.ok ? "✔ حجم أدنى " + v.volumeMin : "✘ " + v.error}`);
  $("taOut").dataset.busy = ""; $("taOut").textContent = `✔ ${res.data.note}\nرموز مشابهة: ${m}\n${lines.join("\n")}`;
  setTimeout(loadState, 600);
};
$("btnClearTa").onclick = async () => {
  if (!confirm("حذف بيانات TickerAll؟")) return;
  report(await postJson("/api/tickerall/clear"), $("taOut"));
};
$("btnSaveKeys").onclick = async () => {
  const res = await postJson("/api/keys", { api_key: $("apiKey").value, api_secret: $("apiSecret").value, market: $("market").value });
  if (res.ok) { $("apiKey").value = ""; $("apiSecret").value = ""; }
  report(res, $("testOut"));
};
$("btnTest").onclick = async () => { $("testOut").textContent = "جارٍ الاختبار..."; report(await postJson("/api/broker/test"), $("testOut")); };
$("btnClearKeys").onclick = async () => {
  if (!confirm("حذف مفاتيح Binance؟")) return;
  report(await postJson("/api/keys/clear"), $("testOut"));
};
$("taSymGold").oninput = () => $("taSymGold").dataset.touched = "1";
$("taSymBtc").oninput = () => $("taSymBtc").dataset.touched = "1";

// ---------- boot ----------
loadState();
loadMarket();
setInterval(loadState, 5000);
setInterval(() => loadMarket(false), 10000);
</script>
</body>
</html>
'''
