"""مركز التحليل الذاتي: ملف واحد يعمل مباشرة على Render.
يشمل: جلب البيانات، الاختبار التاريخي، البحث التطوري، التنفيذ الورقي والتجريبي، الجدولة، واللوحة."""
import hashlib
import hmac
import json
import math
import os
import random
import sqlite3
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
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
MIN_VAL_TRADES = 8
MIN_PF = 1.1
MAX_VAL_DD_R = 15.0
DISCLAIMER = "⚠️ تنفيذ تعليمي، وليس توصية مالية."
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "data" / "bot.db"))

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

# =====================================================================
# الاختبار التاريخي والبحث التطوري
# =====================================================================
SPACE = {
    "atr_len": [7, 14, 21],
    "depth_mult": [0.05, 0.1, 0.2, 0.3, 0.5],
    "sl_buffer": [0.0, 0.1, 0.2, 0.3],
    "target_r": [1.0, 1.5, 2.0, 2.5, 3.0],
    "tf": [1, 2, 4],
    "trend": [False, True],
}
BASELINE = {"atr_len": 14, "depth_mult": 0.2, "sl_buffer": 0.2, "target_r": 2.0, "tf": 1, "trend": False}


def key(genome: dict) -> str:
    return json.dumps(genome, sort_keys=True)


def random_genome(rng: random.Random) -> dict:
    return {k: rng.choice(v) for k, v in SPACE.items()}


def mutate(genome: dict, rng: random.Random) -> dict:
    child = dict(genome)
    for k in rng.sample(list(SPACE), k=rng.choice([1, 2])):
        child[k] = rng.choice(SPACE[k])
    return child


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


def describe(g: dict) -> str:
    return (f"ATR {g['atr_len']} | عمق {g['depth_mult']} | بفر وقف {g['sl_buffer']} | "
            f"هدف {g['target_r']}R | إطار {g['tf']}H | فلتر EMA200 {'نعم' if g['trend'] else 'لا'}")


def fmt(v) -> str:
    return "—" if v is None else f"{v:,.2f}"


def load_market(asset: str):
    info = ASSETS[asset]
    h1 = completed_only(fetch_history(info["symbol"], "1h", HISTORY_H1))
    h1["open_ms"] = [int(t.timestamp() * 1000) for t in h1["open_time"]]
    h1["close_ms"] = [int(t.timestamp() * 1000) for t in h1["close_time"]]
    daily = completed_only(fetch_history(info["symbol"], "1d", HISTORY_D1))
    return h1, daily


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
        engine.update(status="analyzing", message="جارٍ تحليل السوق والبحث عن فرص...")
        log_event("scan", "بدء فحص الأصول")
        for asset in store.get_assets():
            try:
                res = trading_cycle(asset, store, send_alerts=send_alerts)
                live[asset] = {"price": res["price"], "latest": res["latest"],
                               "has_champion": res["has_champion"], "errors": res["errors"],
                               "note": res["note"], "updated": int(time.time()),
                               "ta_positions": res["ta_positions"], "ta_account": res["ta_account"]}
                for t in res.get("closed") or []:
                    log_event("close", f"إغلاق صفقة {t.get('side', '')} · {_fmt(t.get('r'))}R", asset)
                if res.get("opened"):
                    o = res["opened"]
                    log_event("open", f"فتح صفقة {o.get('side', '')} عند {_fmt(o.get('entry'))}", asset)
                for e in res["errors"]:
                    log_event("warn", e, asset)
                if not res.get("opened") and not res["errors"]:
                    if not res["has_champion"]:
                        log_event("info", "لا توجد استراتيجية معتمدة، التداول متوقف لهذا الأصل", asset)
                    elif res.get("note"):
                        log_event("info", res["note"], asset)
                    else:
                        log_event("info", "فُحص السوق: لا توجد إشارة جديدة", asset)
            except Exception as exc:
                traceback.print_exc()
                live[asset] = {"error": str(exc), "updated": int(time.time())}
                log_event("error", f"خطأ في الفحص: {str(exc)[:160]}", asset)
        engine["cycles"] += 1
        engine["last_run"] = int(time.time())
        engine.update(status="idle", message="بانتظار الفحص التالي")


def run_evolution_job(send_alerts=True):
    with work_lock:
        engine.update(status="analyzing", message="جارٍ البحث التطوري عن استراتيجيات أفضل...")
        for asset in store.get_assets():
            try:
                res = evolve_asset(asset, store, CONFIG["evolve_generations"], CONFIG["evolve_population"], send_alerts)
                log_event("evolve", "اعتُمدت استراتيجية جديدة" if res.get("changed") else "لم تتغير الاستراتيجية المعتمدة", asset)
                engine.update(status="idle", message="بانتظار الفحص التالي")
            except Exception:
                traceback.print_exc()
        meta["last_evolve"] = time.time()


def _scheduler():
    while True:
        run_trading()
        if time.time() - meta["last_evolve"] >= CONFIG["evolve_every_hours"] * 3600:
            run_evolution_job()
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
        meta["ta_test"] = {"ok": True, "at": int(time.time()), "note": "اتصال تلقائي عند التشغيل"}
        print(f"[tickerall] auto-connected, balance={info.get('balance')} {info.get('currency')}", flush=True)
        if os.getenv("DEFAULT_MODE") == "tickerall":
            store.set_mode("tickerall")
    except TickerAllError as exc:
        meta["ta_test"] = {"ok": False, "error": str(exc)[:200], "at": int(time.time())}
        print(f"[tickerall] auto-connect failed: {exc}", flush=True)
    except Exception as exc:
        meta["ta_test"] = {"ok": False, "error": "فشل الاتصال التلقائي", "at": int(time.time())}
        print(f"[tickerall] auto-connect error: {type(exc).__name__}", flush=True)


if os.getenv("DISABLE_SCHEDULER") != "1":
    threading.Thread(target=ta_autoconnect, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()


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
                     if (live.get(a) or {}).get("ta_account")), None)
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
                     "session_open": in_session(a), "live": live.get(a, {})}
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
        "engine": {**engine, "mode": store.get_mode(), "scan_minutes": CONFIG["check_every_minutes"]},
        "activity": list(activity)[:40],
    }


@app.get("/health")
def health():
    return "ok"


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


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
    threading.Thread(target=run_trading, daemon=True).start()
    return jsonify({"ok": True, "note": "بدأ الفحص"})


@app.post("/api/evolve")
def evolve_now():
    if not _authorized():
        return _deny()
    threading.Thread(target=run_evolution_job, daemon=True).start()
    return jsonify({"ok": True, "note": "بدأ التطوير، قد يستغرق عدة دقائق"})


INDEX_HTML = r'''
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>مركز التحليل الذاتي</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #05070b;
    --panel: rgba(14, 19, 29, .62);
    --line: rgba(255, 255, 255, .07);
    --text: #e6ebf3;
    --muted: #7c8799;
    --gold: #f5c451;
    --emerald: #34d399;
    --rose: #fb7185;
    --sky: #38bdf8;
  }
  html, body { background: var(--bg); }
  body {
    font-family: "IBM Plex Sans Arabic", "Segoe UI", system-ui, sans-serif;
    color: var(--text);
    background-image:
      radial-gradient(900px 500px at 85% -10%, rgba(52, 211, 153, .10), transparent 60%),
      radial-gradient(800px 480px at 0% 0%, rgba(245, 196, 81, .08), transparent 55%),
      linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px);
    background-size: auto, auto, 44px 44px, 44px 44px;
    background-attachment: fixed;
    -webkit-font-smoothing: antialiased;
  }
  .num { font-family: "JetBrains Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
  .glass {
    background: linear-gradient(180deg, rgba(20, 27, 40, .72), rgba(8, 11, 18, .72));
    backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    border: 1px solid var(--line); border-radius: 22px;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 30px 60px -30px rgba(0,0,0,.8);
  }
  .ring-gold    { box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 0 0 1px rgba(245,196,81,.18), 0 20px 50px -20px rgba(245,196,81,.25); }
  .ring-emerald { box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 0 0 1px rgba(52,211,153,.18), 0 20px 50px -20px rgba(52,211,153,.25); }
  .ring-rose    { box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 0 0 1px rgba(251,113,133,.18), 0 20px 50px -20px rgba(251,113,133,.25); }
  .ring-sky     { box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 0 0 1px rgba(56,189,248,.16), 0 20px 50px -20px rgba(56,189,248,.22); }
  .eyebrow { font-size: 11px; letter-spacing: .08em; color: var(--muted); text-transform: uppercase; }
  .title-grad { background: linear-gradient(95deg, #fff 0%, var(--gold) 55%, var(--emerald) 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }

  .pulse { width: 8px; height: 8px; border-radius: 999px; background: var(--emerald); box-shadow: 0 0 0 0 rgba(52,211,153,.6); animation: ping 2s infinite; }
  .pulse.off { background: #475569; animation: none; box-shadow: none; }
  @keyframes ping { 0% { box-shadow: 0 0 0 0 rgba(52,211,153,.55); } 70% { box-shadow: 0 0 0 10px rgba(52,211,153,0); } 100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); } }

  .btn { display: inline-flex; align-items: center; justify-content: center; gap: .4rem; border-radius: 14px; padding: .55rem 1rem; font-size: .85rem; font-weight: 600; color: var(--text); background: rgba(255,255,255,.04); border: 1px solid var(--line); transition: all .18s ease; white-space: nowrap; }
  .btn:hover { background: rgba(255,255,255,.09); transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn-primary { color: #05070b; background: linear-gradient(120deg, var(--gold), #fb923c 45%, var(--emerald)); border: none; box-shadow: 0 10px 30px -10px rgba(245,196,81,.55); }
  .btn-primary:hover { filter: brightness(1.07); background: linear-gradient(120deg, var(--gold), #fb923c 45%, var(--emerald)); }
  .btn-danger { color: #fecdd3; background: rgba(244,63,94,.10); border-color: rgba(251,113,133,.25); }
  .btn-danger:hover { background: rgba(244,63,94,.18); }

  .inp { width: 100%; background: rgba(3,5,9,.7); border: 1px solid var(--line); border-radius: 14px; padding: .6rem .8rem; font-size: .88rem; color: var(--text); outline: none; transition: border-color .18s, box-shadow .18s; }
  .inp::placeholder { color: #4b5563; }
  .inp:focus { border-color: rgba(245,196,81,.55); box-shadow: 0 0 0 4px rgba(245,196,81,.10); }
  .inp.bad { border-color: rgba(251,113,133,.8); box-shadow: 0 0 0 4px rgba(251,113,133,.12); }
  .label { display: block; font-size: .75rem; color: var(--muted); margin-bottom: .35rem; }

  .seg { display: inline-flex; gap: 4px; padding: 4px; border-radius: 16px; background: rgba(3,5,9,.7); border: 1px solid var(--line); overflow-x: auto; max-width: 100%; }
  .seg-btn { padding: .5rem 1.1rem; border-radius: 12px; font-size: .86rem; font-weight: 600; color: var(--muted); white-space: nowrap; transition: all .18s; }
  .seg-btn:hover { color: var(--text); }
  .seg-btn.active { color: #05070b; background: linear-gradient(120deg, var(--gold), var(--emerald)); box-shadow: 0 8px 20px -8px rgba(245,196,81,.6); }

  .badge { display: inline-flex; align-items: center; gap: .35rem; font-size: .72rem; font-weight: 600; border-radius: 999px; padding: .2rem .65rem; border: 1px solid var(--line); background: rgba(255,255,255,.03); color: var(--muted); white-space: nowrap; }
  .b-up { color: var(--emerald); border-color: rgba(52,211,153,.35); background: rgba(52,211,153,.08); }
  .b-down { color: var(--rose); border-color: rgba(251,113,133,.35); background: rgba(251,113,133,.08); }
  .b-gold { color: var(--gold); border-color: rgba(245,196,81,.35); background: rgba(245,196,81,.08); }
  .b-sky { color: var(--sky); border-color: rgba(56,189,248,.35); background: rgba(56,189,248,.08); }

  .bar { height: 6px; border-radius: 999px; background: rgba(255,255,255,.06); overflow: hidden; }
  .bar > span { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--emerald), var(--sky)); transition: width .6s ease; }

  .tbl { width: 100%; border-collapse: separate; border-spacing: 0; }
  .tbl th { color: var(--muted); font-weight: 500; font-size: .74rem; padding: .6rem .6rem; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
  .tbl td { padding: .7rem .6rem; font-size: .84rem; border-bottom: 1px solid rgba(255,255,255,.04); white-space: nowrap; }
  .tbl tr:hover td { background: rgba(255,255,255,.02); }

  .up { color: var(--emerald); }
  .down { color: var(--rose); }
  .flat { color: #cbd5e1; }
  .card-title { font-weight: 700; font-size: 1rem; }
  details > summary { list-style: none; }
  details > summary::-webkit-details-marker { display: none; }
  .fade-in { animation: fade .45s ease both; }
  @keyframes fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .radar { position: relative; width: 56px; height: 56px; flex: none; }
  .radar i { position: absolute; inset: 0; border-radius: 999px; border: 1px solid rgba(52,211,153,.5); opacity: 0; }
  .radar b { position: absolute; inset: 38%; border-radius: 999px; background: #475569; }
  .radar.on b { background: #34d399; box-shadow: 0 0 14px #34d399; }
  .radar.on i { animation: ripple 1.8s ease-out infinite; }
  .radar.on i:nth-child(2) { animation-delay: .6s; }
  .radar.on i:nth-child(3) { animation-delay: 1.2s; }
  @keyframes ripple { 0% { transform: scale(.35); opacity: .9; } 100% { transform: scale(1.5); opacity: 0; } }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,.08); border-radius: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body class="min-h-screen">

<div class="max-w-7xl mx-auto px-3 md:px-6 pb-10">

  <!-- ============ Top bar ============ -->
  <header class="sticky top-0 z-20 -mx-3 md:-mx-6 px-3 md:px-6 pt-3 pb-3 backdrop-blur-xl bg-[#05070b]/70 border-b border-white/5">
    <div class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
      <div class="flex items-center gap-3">
        <div class="w-11 h-11 rounded-2xl flex items-center justify-center text-lg font-bold text-[#05070b]" style="background:linear-gradient(135deg,#f5c451,#34d399);box-shadow:0 10px 30px -8px rgba(245,196,81,.5)">MA</div>
        <div>
          <h1 class="title-grad text-lg md:text-xl font-extrabold leading-tight">مركز التحليل الذاتي</h1>
          <p class="flex items-center gap-2 text-[12px] text-[var(--muted)] mt-0.5">
            <span id="liveDot" class="pulse"></span><span id="liveText">جارٍ الاتصال...</span>
          </p>
        </div>
      </div>
      <div class="flex flex-col sm:flex-row gap-2">
        <input id="pw" type="password" placeholder="كلمة مرور الإدارة" autocomplete="current-password" class="inp sm:w-48">
        <select id="scope" class="inp sm:w-44">
          <option value="all">الذهب + البيتكوين</option>
          <option value="XAUUSD">الذهب فقط</option>
          <option value="BTCUSDT">البيتكوين فقط</option>
        </select>
        <div class="flex gap-2">
          <button id="btnScope" class="btn">حفظ الأصول</button>
          <button id="btnRun" class="btn">فحص</button>
          <button id="btnEvolve" class="btn btn-primary">تطوير</button>
        </div>
      </div>
    </div>
    <p id="msg" class="text-xs text-amber-200 min-h-[1rem] mt-2 px-1"></p>
  </header>

  <!-- ============ Tabs ============ -->
  <nav class="mt-4 mb-5">
    <div class="seg">
      <button class="seg-btn active" data-tab="overview">الرئيسية</button>
      <button class="seg-btn" data-tab="brokers">الوسطاء والإعدادات</button>
      <button class="seg-btn" data-tab="research">التطوير والسجل</button>
    </div>
  </nav>

  <!-- ============ Tab: overview ============ -->
  <section data-pane="overview" class="space-y-5 fade-in">
    <div class="glass ring-emerald p-5 md:p-6">
      <div class="flex flex-col md:flex-row md:items-center gap-5 md:justify-between">
        <div class="flex items-center gap-4">
          <div id="radar" class="radar"><i></i><i></i><i></i><b></b></div>
          <div>
            <div class="eyebrow">محرك التداول الآلي · قواعد تحليل + بحث تطوري بوابات تحقق</div>
            <div id="engMsg" class="text-lg font-bold mt-1">بانتظار أول فحص</div>
            <div class="text-xs text-[var(--muted)] mt-1"><span id="engLast"></span> · <span id="engNext" class="num"></span></div>
          </div>
        </div>
        <div class="flex gap-8">
          <div><div class="eyebrow">الفحوصات</div><div id="engCycles" class="num text-xl font-bold mt-1">0</div></div>
          <div><div class="eyebrow">الوضع</div><div id="engMode" class="text-sm font-semibold mt-2">—</div></div>
        </div>
      </div>
      <div class="mt-5 border-t border-white/5 pt-4">
        <div class="eyebrow mb-2">سجل النشاط الحي</div>
        <ul id="engFeed" class="space-y-2 max-h-72 overflow-y-auto"></ul>
      </div>
    </div>

    <div id="kpis" class="grid grid-cols-2 xl:grid-cols-4 gap-3 md:gap-4"></div>

    <div class="glass ring-emerald p-5">
      <div class="flex items-center justify-between mb-3">
        <div>
          <h2 class="card-title">منحنى الرصيد</h2>
          <p class="text-xs text-[var(--muted)]">الرصيد بعد كل صفقة مغلقة</p>
        </div>
        <span id="equityMeta" class="badge"></span>
      </div>
      <div class="relative h-64 md:h-72"><canvas id="equity"></canvas></div>
      <p id="equityEmpty" class="text-[var(--muted)] text-sm text-center py-10 hidden">لا توجد صفقات مغلقة بعد.</p>
    </div>

    <div>
      <h2 class="eyebrow mb-3 px-1">الأصول</h2>
      <div id="assets" class="grid gap-4 lg:grid-cols-2"></div>
    </div>

    <div class="grid gap-4 xl:grid-cols-2">
      <div class="glass ring-sky p-5">
        <div class="flex items-center justify-between mb-3">
          <h2 class="card-title">الصفقات المفتوحة</h2>
          <span id="openCount" class="badge b-sky">0</span>
        </div>
        <div class="overflow-x-auto"><table class="tbl">
          <thead><tr><th>الأصل</th><th>الاتجاه</th><th>الدخول</th><th>الوقف</th><th>الهدف</th><th>الوضع</th></tr></thead>
          <tbody id="openRows"></tbody>
        </table></div>
        <div id="unmanagedBox" class="mt-5 hidden">
          <div class="text-xs font-semibold text-amber-300 mb-2">مراكز على المنصة غير مُدارة من النظام</div>
          <div class="overflow-x-auto"><table class="tbl">
            <thead><tr><th>الرمز</th><th>الاتجاه</th><th>الحجم</th><th>الربح</th><th>الوقف</th><th>الهدف</th></tr></thead>
            <tbody id="unmanagedRows"></tbody>
          </table></div>
        </div>
      </div>
      <div class="glass p-5">
        <div class="flex items-center justify-between mb-3">
          <h2 class="card-title">الصفقات المغلقة</h2>
          <span class="badge">آخر 60</span>
        </div>
        <div class="overflow-auto max-h-96"><table class="tbl">
          <thead class="sticky top-0 bg-[#0a0e16]"><tr><th>الأصل</th><th>الاتجاه</th><th>R</th><th>الربح</th><th>الإغلاق</th></tr></thead>
          <tbody id="closedRows"></tbody>
        </table></div>
      </div>
    </div>
  </section>

  <!-- ============ Tab: brokers ============ -->
  <section data-pane="brokers" class="hidden space-y-5 fade-in">

    <div class="glass p-5">
      <div class="mb-4">
        <h2 class="card-title">وضع التنفيذ</h2>
        <p class="text-xs text-[var(--muted)]">اختر الوضع ثم اضغط تطبيق. الوضع الحقيقي غير مفعّل في هذه النسخة.</p>
      </div>
      <div id="modes" class="grid gap-3 md:grid-cols-4"></div>
      <div class="flex flex-wrap items-center gap-3 mt-4">
        <button id="btnSetMode" class="btn btn-primary">تطبيق الوضع المختار</button>
        <span id="modeOut" class="text-xs text-[var(--muted)]"></span>
      </div>
    </div>

    <div class="glass ring-gold p-5 space-y-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="card-title">TickerAll · MT5 تجريبي (Exness)</h2>
          <p class="text-xs text-[var(--muted)] mt-1">الوقف والهدف يُحملان على جهة الوسيط. الحسابات الحقيقية مرفوضة.</p>
        </div>
        <span id="taBadge" class="badge">غير مرتبط</span>
      </div>

      <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <label><span class="label">API Key</span>
          <input id="taKey" type="text" autocomplete="off" spellcheck="false" class="inp" placeholder="cf_api_…"></label>
        <label><span class="label">رقم الحساب التجريبي</span>
          <input id="taLogin" type="text" inputmode="numeric" autocomplete="off" class="inp"></label>
        <label><span class="label">السيرفر</span>
          <input id="taServer" type="text" autocomplete="off" class="inp" placeholder="Exness-MT5Trial16"></label>
        <label><span class="label">كلمة مرور الحساب التجريبي</span>
          <input id="taPass" type="password" autocomplete="new-password" class="inp"></label>
        <label><span class="label">رمز الذهب لدى الوسيط</span>
          <input id="taSymGold" type="text" class="inp" value="XAUUSD"></label>
        <label><span class="label">رمز البيتكوين لدى الوسيط</span>
          <input id="taSymBtc" type="text" class="inp" value="BTCUSD"></label>
      </div>
      <p class="text-[11px] text-[var(--muted)]">الحقول أعلاه لحساب المنصة. كلمة مرور الإدارة في أعلى الصفحة حقل منفصل يحمي الحفظ.</p>

      <div class="flex flex-wrap gap-2">
        <button id="btnSaveTa" class="btn btn-primary">حفظ</button>
        <button id="btnTestTa" class="btn">اتصال واختبار الرموز</button>
        <button id="btnClearTa" class="btn btn-danger">حذف</button>
      </div>
      <pre id="taOut" class="num text-xs text-slate-300 whitespace-pre-wrap rounded-2xl bg-black/30 border border-white/5 p-3 min-h-[3rem]"></pre>
    </div>

    <details class="glass p-5">
      <summary class="flex flex-wrap items-center justify-between gap-2">
        <span class="card-title">Binance Testnet <span class="text-xs text-[var(--muted)] font-normal">· للبيتكوين، أموال وهمية</span></span>
        <span id="keyStatus" class="text-xs text-[var(--muted)]"></span>
      </summary>
      <div class="grid gap-4 md:grid-cols-4 mt-5">
        <label><span class="label">نوع السوق</span>
          <select id="market" class="inp"></select></label>
        <label><span class="label">API Key</span>
          <input id="apiKey" type="text" autocomplete="off" spellcheck="false" class="inp"></label>
        <label><span class="label">API Secret</span>
          <input id="apiSecret" type="password" autocomplete="off" class="inp"></label>
        <div class="flex items-end gap-2">
          <button id="btnSaveKeys" class="btn btn-primary flex-1">حفظ</button>
          <button id="btnTest" class="btn flex-1">اختبار</button>
          <button id="btnClearKeys" class="btn btn-danger">حذف</button>
        </div>
      </div>
      <p id="testOut" class="text-xs text-slate-300 mt-4"></p>
    </details>
  </section>

  <!-- ============ Tab: research ============ -->
  <section data-pane="research" class="hidden grid gap-4 xl:grid-cols-3 fade-in">
    <div class="glass p-5 xl:col-span-2">
      <h2 class="card-title mb-3">لوحة المتصدرين</h2>
      <div class="overflow-x-auto"><table class="tbl">
        <thead><tr><th>الأصل</th><th>الاستراتيجية</th><th>نجاح%</th><th>PF</th><th>تراجع R</th><th>الدرجة</th></tr></thead>
        <tbody id="boardRows"></tbody>
      </table></div>
    </div>
    <div class="glass p-5">
      <h2 class="card-title mb-3">سجل التطوير</h2>
      <ul id="evoLog" class="space-y-2 text-sm max-h-96 overflow-y-auto"></ul>
    </div>
  </section>

  <footer class="text-[11px] text-[var(--muted)] leading-relaxed px-2 mt-10">
    ⚠️ نظام تعليمي يعمل بتنفيذ ورقي أو على حسابات تجريبية فقط. نتائج الاختبار التاريخي لا تضمن أرباحاً مستقبلية، والبحث التطوري قد يُفرط في التخصيص رغم التحقق خارج العينة.
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, d = 2) => (v == null || isNaN(v)) ? "—" : Number(v).toLocaleString("en-US", {maximumFractionDigits: d, minimumFractionDigits: d});
const dt = (s) => s ? String(s).slice(0, 16).replace("T", " ") : "—";
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
const sideTxt = (s) => (String(s).toLowerCase() === "buy" ? '<span class="up">شراء</span>' : '<span class="down">بيع</span>');
const modeTxt = (m) => ({paper: "ورقي", demo: "Binance", tickerall: "MT5"}[m] || esc(m || "—"));
const describe = (g) => g ? `ATR ${g.atr_len} · عمق ${g.depth_mult} · بفر ${g.sl_buffer} · ${g.target_r}R · ${g.tf}H · EMA200 ${g.trend ? "✔" : "✘"}` : "";

// ---------- admin password: remembered in this browser ----------
const PW_KEY = "ma_admin_pw";
try { $("pw").value = localStorage.getItem(PW_KEY) || ""; } catch (e) {}
$("pw").addEventListener("input", () => { try { localStorage.setItem(PW_KEY, $("pw").value); } catch (e) {} });

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll(".seg-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("[data-pane]").forEach(p => p.classList.toggle("hidden", p.dataset.pane !== name));
  try { localStorage.setItem("ma_tab", name); } catch (e) {}
}
document.querySelectorAll(".seg-btn").forEach(b => b.onclick = () => showTab(b.dataset.tab));
showTab((() => { try { return localStorage.getItem("ma_tab") || "overview"; } catch (e) { return "overview"; } })());

let chart = null;
let lastState = null;

function msg(t) { $("msg").textContent = t; }

async function load() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    lastState = await r.json();
    render(lastState);
    $("liveDot").classList.remove("off");
    $("liveText").textContent = "متصل · آخر تحديث " + new Date().toLocaleTimeString("ar-SA");
  } catch (e) {
    $("liveDot").classList.add("off");
    $("liveText").textContent = "انقطع الاتصال بالخادم، يُعاد المحاولة...";
  }
}

function kpi(label, value, sub, ring, extra = "") {
  return `<div class="glass ${ring} p-4 md:p-5 fade-in">
    <div class="eyebrow">${label}</div>
    <div class="num text-2xl md:text-3xl font-bold mt-2">${value}</div>
    <div class="text-[11px] text-[var(--muted)] mt-1.5">${sub}</div>${extra}
  </div>`;
}

function render(s) {
  const a = s.account;
  const barW = Math.max(0, Math.min(100, a.win_rate || 0));
  $("kpis").innerHTML = [
    kpi("الرصيد", `$${num(a.balance)}`,
        a.source === "tickerall" ? "رصيد الحساب التجريبي على الوسيط · مباشر"
        : a.source === "tickerall-pending" ? "بانتظار أول قراءة من الوسيط"
        : `ورقي · البداية $${num(a.start, 0)}`, "ring-gold"),
    kpi("العائد", `<span class="${tone(a.return_pct)}">${num(a.return_pct)}%</span>`, `${a.trades} صفقة مغلقة`, a.return_pct >= 0 ? "ring-emerald" : "ring-rose"),
    kpi("نسبة النجاح", `${num(a.win_rate, 1)}%`, "من الصفقات المغلقة", "ring-sky", `<div class="bar mt-3"><span style="width:${barW}%"></span></div>`),
    kpi("معامل الربح", num(a.profit_factor), "الأرباح ÷ الخسائر", "ring-emerald"),
  ].join("");
  drawEquity(s.equity, a.start);
  renderEngine(s.engine, s.activity);
  renderAssets(s.assets);
  renderOpen(s);
  renderClosed(s.closed);
  renderBoard(s.assets);
  renderEvolutions(s.evolutions);
  renderSettings(s.settings);
  $("openCount").textContent = Object.values(s.assets).reduce((n, v) => n + (v.open || []).length, 0);
}

function renderAssets(assets) {
  $("assets").innerHTML = Object.entries(assets).map(([k, v]) => {
    const L = (v.live && v.live.latest) || {};
    const champ = v.champion;
    const err = v.live && v.live.error ? `<p class="text-rose-400 text-xs">⚠ ${esc(v.live.error)}</p>` : "";
    const sess = v.session_open
      ? '<span class="badge b-up">● السوق مفتوح</span>'
      : '<span class="badge b-down">● السوق مغلق</span>';
    const champBlock = champ ? `
      <div class="rounded-2xl bg-black/30 border border-amber-400/15 p-4 text-xs space-y-1.5">
        <div class="text-amber-300 font-semibold">الاستراتيجية المعتمدة</div>
        <div class="text-slate-300 num">${esc(describe(champ.genome))}</div>
        <div class="text-[var(--muted)]">تحقق: نجاح ${num(champ.result.val.win_rate,1)}% · PF ${num(champ.result.val.profit_factor)} · تراجع ${num(champ.result.val.max_dd_r)}R · ${champ.result.val.trades} صفقة</div>
        <div class="text-slate-500">منذ ${dt(champ.since)}</div>
      </div>` : `<div class="rounded-2xl bg-rose-500/10 border border-rose-500/20 p-4 text-xs text-rose-300">لا توجد استراتيجية معتمدة، التداول متوقف لهذا الأصل.</div>`;
    return `<div class="glass ${champ ? "ring-gold" : ""} p-5 space-y-4 fade-in">
      <div class="flex items-start justify-between gap-3">
        <div>
          <h3 class="card-title text-amber-300">${esc(v.label)}</h3>
          <div class="mt-2">${sess}</div>
        </div>
        <div class="text-left">
          <div class="num text-2xl font-bold">${num(v.live && v.live.price)}</div>
          <div class="text-[10px] text-[var(--muted)]">آخر إغلاق H1</div>
        </div>
      </div>
      ${err}${champBlock}
      <div class="grid grid-cols-2 gap-2 text-xs text-slate-300">
        <div class="rounded-xl bg-white/[.03] border border-white/5 p-3">قمة الأمس <b class="num block text-sm mt-1">${num(L.prev_day_high)}</b></div>
        <div class="rounded-xl bg-white/[.03] border border-white/5 p-3">قاع الأمس <b class="num block text-sm mt-1">${num(L.prev_day_low)}</b></div>
        <div class="col-span-2 rounded-xl bg-white/[.03] border border-white/5 p-3 text-amber-200">بانتظار التأكيد: ${esc(L.pending || "—")}</div>
      </div>
    </div>`;
  }).join("");
}

function renderOpen(s) {
  const rows = Object.values(s.assets).flatMap(v => v.open || []);
  $("openRows").innerHTML = rows.length ? rows.map(t => `
    <tr>
      <td>${esc(t.asset)}</td><td>${sideTxt(t.side)}</td>
      <td class="num">${num(t.entry)}</td><td class="num">${num(t.stop)}</td><td class="num">${num(t.target)}</td>
      <td><span class="badge">${modeTxt(t.mode)}</span></td>
    </tr>`).join("") : `<tr><td colspan="6" class="p-6 text-[var(--muted)] text-center">لا توجد صفقات مفتوحة</td></tr>`;

  const unmanaged = Object.values(s.assets).flatMap(v => ((v.live && v.live.ta_positions) || []).filter(p => !p.managed));
  $("unmanagedBox").classList.toggle("hidden", unmanaged.length === 0);
  $("unmanagedRows").innerHTML = unmanaged.map(p => `
    <tr><td>${esc(p.symbol)}</td><td>${sideTxt(p.side)}</td><td class="num">${num(p.volume)}</td>
    <td class="num ${tone(p.profit)}">${num(p.profit)}</td><td class="num">${num(p.stopLoss)}</td><td class="num">${num(p.takeProfit)}</td></tr>`).join("");
}

function renderClosed(closed) {
  $("closedRows").innerHTML = closed.length ? closed.map(t => `
    <tr>
      <td>${esc(t.asset)}</td><td>${sideTxt(t.side)}</td>
      <td class="num ${tone(t.r)}">${num(t.r)}R</td>
      <td class="num ${tone(t.pnl_usd)}">${t.pnl_usd > 0 ? "+" : ""}${num(t.pnl_usd)}$</td>
      <td class="text-[var(--muted)]">${dt(t.closed_utc)}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="p-6 text-[var(--muted)] text-center">لا توجد صفقات مغلقة بعد</td></tr>`;
}

function renderBoard(assets) {
  const board = Object.entries(assets).flatMap(([k, v]) => v.leaderboard.map(x => ({...x, asset: v.label})));
  $("boardRows").innerHTML = board.length ? board.map(x => `
    <tr>
      <td>${esc(x.asset)}</td>
      <td class="text-xs text-slate-300 num">${esc(describe(x.genome))}</td>
      <td class="num">${num(x.result.val.win_rate, 1)}</td>
      <td class="num">${num(x.result.val.profit_factor)}</td>
      <td class="num">${num(x.result.val.max_dd_r)}</td>
      <td class="num text-amber-300 font-semibold">${num(x.score, 3)}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="p-6 text-[var(--muted)] text-center">لم تُعتمد أي استراتيجية بعد. اضغط "تطوير".</td></tr>`;
}

function renderEvolutions(list) {
  $("evoLog").innerHTML = list.length ? list.map(e => `
    <li class="rounded-2xl bg-black/30 border border-white/5 p-3">
      <div class="flex justify-between text-xs text-[var(--muted)]"><span>${esc(e.asset)}</span><span class="num">${dt(e.created_utc)}</span></div>
      <div class="mt-1.5 text-sm">جُرّب ${e.evaluated} · نجح ${e.accepted}</div>
      <div class="text-xs text-[var(--muted)] mt-0.5">${esc(e.note)}</div>
    </li>`).join("") : `<li class="text-[var(--muted)]">لا يوجد سجل بعد</li>`;
}

function drawEquity(points, start) {
  const empty = points.length === 0;
  $("equityEmpty").classList.toggle("hidden", !empty);
  $("equityMeta").textContent = empty ? "" : `${points.length} صفقة مغلقة`;
  if (empty) { if (chart) { chart.destroy(); chart = null; } return; }
  const labels = ["البداية", ...points.map(p => dt(p.t))];
  const data = [start, ...points.map(p => p.bal)];
  if (!chart) {
    const ctx = $("equity").getContext("2d");
    const grad = ctx.createLinearGradient(0, 0, 0, 280);
    grad.addColorStop(0, "rgba(52,211,153,.35)");
    grad.addColorStop(1, "rgba(52,211,153,0)");
    chart = new Chart($("equity"), {
      type: "line",
      data: { labels, datasets: [{ label: "الرصيد", data, borderColor: "#34d399", backgroundColor: grad,
        fill: true, tension: 0.35, pointRadius: 0, pointHoverRadius: 5, borderWidth: 2.5 }] },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false },
          tooltip: { backgroundColor: "rgba(5,7,11,.95)", borderColor: "rgba(52,211,153,.35)", borderWidth: 1,
                     titleColor: "#e6ebf3", bodyColor: "#a7f3d0", padding: 12, displayColors: false,
                     callbacks: { label: c => `$${num(c.parsed.y)}` } } },
        scales: { x: { ticks: { color: "#64748b", maxTicksLimit: 8 }, grid: { color: "rgba(255,255,255,.03)" } },
                  y: { ticks: { color: "#64748b", callback: v => "$" + num(v, 0) }, grid: { color: "rgba(255,255,255,.05)" } } } },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = data;
    chart.update();
  }
}

let pickedMode = null;
function renderSettings(st) {
  if (!st) return;
  const cur = st.mode;
  $("modes").innerHTML = st.modes.map(m => `
    <label class="block rounded-2xl border p-4 transition ${m.available ? "border-white/10 bg-white/[.02] cursor-pointer hover:border-amber-400/40" : "border-white/5 opacity-40"}">
      <div class="flex items-center gap-2">
        <input type="radio" name="mode" value="${esc(m.key)}" ${m.available ? "" : "disabled"} ${(pickedMode || cur) === m.key ? "checked" : ""} class="accent-amber-400">
        <span class="font-semibold text-sm">${esc(m.name)}</span>
        ${cur === m.key ? '<span class="badge b-up">مفعّل</span>' : ""}
      </div>
      <p class="text-[11px] text-[var(--muted)] mt-2">${esc(m.note)}</p>
    </label>`).join("");
  document.querySelectorAll('input[name="mode"]').forEach(el => el.onchange = () => { pickedMode = el.value; });

  $("keyStatus").textContent = st.keys.present ? `🔑 ${st.keys.masked}` : "";
  if (!$("market").options.length) $("market").innerHTML = st.markets.map(m => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
  if (st.last_test) $("testOut").textContent = st.last_test.ok ? `✔ آخر اختبار ناجح: ${num(st.last_test.usdt_free)} USDT` : `✘ آخر اختبار فشل: ${st.last_test.error}`;

  const ta = st.tickerall;
  $("taBadge").textContent = ta.present ? (ta.session ? "متصل ●" : "محفوظ · غير متصل") : "غير مرتبط";
  $("taBadge").className = "badge " + (ta.present ? (ta.session ? "b-up" : "b-gold") : "");
  if (!$("taSymGold").dataset.touched) $("taSymGold").value = ta.symbols.XAUUSD;
  if (!$("taSymBtc").dataset.touched) $("taSymBtc").value = ta.symbols.BTCUSDT;
  if (!$("taServer").value && ta.server) $("taServer").value = ta.server;
  $("taOut").textContent = ta.present
    ? `مفتاح: ${ta.api_key_masked} · حساب: ${ta.login_masked} · سيرفر: ${ta.server}` +
      (ta.session ? `\nالجلسة نشطة حتى ${dt(ta.session.expiresAt)}` : "\nلا توجد جلسة نشطة، اضغط اتصال واختبار.") +
      (ta.last_test ? `\n${ta.last_test.ok ? "✔ " + ta.last_test.note : "✘ " + ta.last_test.error}` : "")
    : "لم تُحفظ بيانات TickerAll بعد.";
}

// ---------- engine panel ----------
let nextRunAt = null;
const KIND = { open: "b-up", close: "b-gold", warn: "b-down", error: "b-down", evolve: "b-sky", scan: "b-sky", info: "" };
const MODE_AR = { paper: "ورقي", tickerall: "MT5 تجريبي", demo: "Binance Testnet" };
function renderEngine(e, acts) {
  if (!e) return;
  $("radar").classList.toggle("on", e.status === "analyzing");
  $("engMsg").textContent = e.message;
  $("engLast").textContent = e.last_run ? "آخر فحص " + new Date(e.last_run * 1000).toLocaleTimeString("ar-SA") : "لم يبدأ الفحص بعد";
  $("engCycles").textContent = e.cycles;
  $("engMode").textContent = MODE_AR[e.mode] || e.mode;
  nextRunAt = e.next_run ? e.next_run * 1000 : null;
  $("engFeed").innerHTML = acts.length ? acts.map(a => `
    <li class="flex items-start gap-3 text-xs rounded-xl bg-white/[.02] border border-white/5 p-2.5">
      <span class="num text-[var(--muted)] shrink-0">${new Date(a.t * 1000).toLocaleTimeString("ar-SA")}</span>
      <span class="badge ${KIND[a.kind] || ""} shrink-0">${esc(a.asset || "النظام")}</span>
      <span class="text-slate-300">${esc(a.text)}</span>
    </li>`).join("") : `<li class="text-[var(--muted)] text-xs">لا توجد أحداث بعد</li>`;
}
function tickCountdown() {
  if (!nextRunAt) { $("engNext").textContent = ""; return; }
  const left = Math.max(0, Math.round((nextRunAt - Date.now()) / 1000));
  $("engNext").textContent = `الفحص التالي خلال ${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
}
setInterval(tickCountdown, 1000);

// ---------- requests ----------
async function postJson(url, body) {
  const pwd = $("pw").value.trim();
  if (!pwd) {
    $("pw").classList.add("bad");
    $("pw").focus();
    return { ok: false, data: { error: "أدخل كلمة مرور الإدارة في أعلى الصفحة أولاً" }, status: 0 };
  }
  $("pw").classList.remove("bad");
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Admin-Password": pwd },
    body: JSON.stringify(body || {}),
  });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) {
    $("pw").classList.add("bad");
    return { ok: false, data: { error: "كلمة مرور الإدارة غير صحيحة (هذه ليست كلمة مرور الحساب التجريبي)" }, status: 401 };
  }
  return { ok: r.ok, data: j, status: r.status };
}

function report(res, el) {
  const text = res.ok ? (res.data.note || "تم ✔") : "✘ " + (res.data.error || ("خطأ " + res.status));
  if (el) el.textContent = text; else msg(text);
  setTimeout(load, 800);
}

$("btnScope").onclick = async () => report(await postJson("/api/config", { assets: $("scope").value }));
$("btnRun").onclick = async () => report(await postJson("/api/run"));
$("btnEvolve").onclick = async () => report(await postJson("/api/evolve"));

$("btnSetMode").onclick = async () => {
  const sel = document.querySelector('input[name="mode"]:checked');
  if (!sel) return report({ ok: false, data: { error: "اختر وضعاً أولاً" } }, $("modeOut"));
  pickedMode = null;
  report(await postJson("/api/settings", { mode: sel.value }), $("modeOut"));
};

$("btnSaveTa").onclick = async () => {
  $("taOut").textContent = "جارٍ الحفظ...";
  const res = await postJson("/api/tickerall", {
    api_key: $("taKey").value, login: $("taLogin").value, server: $("taServer").value,
    password: $("taPass").value,
    symbols: { XAUUSD: $("taSymGold").value, BTCUSDT: $("taSymBtc").value },
  });
  if (res.ok) { $("taKey").value = ""; $("taPass").value = ""; }
  report(res, $("taOut"));
};
$("btnTestTa").onclick = async () => {
  $("taOut").textContent = "جارٍ الاتصال بالوسيط...";
  const res = await postJson("/api/tickerall/test");
  if (!res.ok) return report(res, $("taOut"));
  const m = (res.data.matches || []).join("، ") || "لم يُعثر على رموز ذهب أو بيتكوين";
  const lines = Object.entries(res.data.checks || {}).map(([a, v]) => `${a}: ${v.ok ? "✔ حجم أدنى " + v.volumeMin : "✘ " + v.error}`);
  $("taOut").textContent = `✔ ${res.data.note}\nرموز مشابهة: ${m}\n${lines.join("\n")}`;
  setTimeout(load, 800);
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
$("btnTest").onclick = async () => {
  $("testOut").textContent = "جارٍ الاختبار...";
  report(await postJson("/api/broker/test"), $("testOut"));
};
$("btnClearKeys").onclick = async () => {
  if (!confirm("حذف مفاتيح Binance؟")) return;
  report(await postJson("/api/keys/clear"), $("testOut"));
};

$("taSymGold").oninput = () => $("taSymGold").dataset.touched = "1";
$("taSymBtc").oninput = () => $("taSymBtc").dataset.touched = "1";

load();
setInterval(load, 5000);
</script>
</body>
</html>
'''
