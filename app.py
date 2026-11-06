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
    errors = []

    def on_close(t, price):
        if t.get("mode") == "demo" and t.get("qty"):
            broker_for(store, "demo").close_position(t, price)

    closed, close_errors = update_open(store, asset, h1, on_close=on_close)
    for e in close_errors:
        msg = f"تعذّر إغلاق الصفقة على المنصة (تُعاد المحاولة): {e}"
        errors.append(msg)
        if send_alerts:
            send(f"⚠️ {label}: {msg}")
    if send_alerts:
        for t in closed:
            send(format_close(label, t))

    mode = store.get_mode()
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
                    broker = broker_for(store, mode)
                    if broker.name != "paper" and s["side"] == "sell" and not broker.supports_short:
                        note = "تم تجاهل إشارة بيع: السوق المختار لا يدعم البيع على المكشوف"
                    else:
                        qty, order_id = None, None
                        if broker.name != "paper":
                            ex = broker.place_entry(asset, s["side"], risk_usd(store), s["entry"], s["stop"])
                            qty, order_id = ex["qty"], ex["order_id"]
                        opened = record_open(store, asset, key(g), s, mode, qty, order_id)
                        if opened and send_alerts:
                            send(format_open(label, opened, g, mode))
                except Exception as exc:
                    note = f"تعذّر فتح الصفقة: {exc}"
                    errors.append(note)
                    if send_alerts:
                        send(f"⚠️ {label}: {note}")
    return {"asset": asset, "price": float(h1["close"].iloc[-1]), "latest": latest,
            "opened": opened, "closed": closed, "has_champion": champ is not None,
            "errors": errors, "note": note, "session_open": in_session(asset)}


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


def run_trading(send_alerts=True):
    with work_lock:
        for asset in store.get_assets():
            try:
                res = trading_cycle(asset, store, send_alerts=send_alerts)
                live[asset] = {"price": res["price"], "latest": res["latest"],
                               "has_champion": res["has_champion"], "errors": res["errors"],
                               "note": res["note"], "updated": int(time.time())}
            except Exception as exc:
                traceback.print_exc()
                live[asset] = {"error": str(exc), "updated": int(time.time())}


def run_evolution_job(send_alerts=True):
    with work_lock:
        for asset in store.get_assets():
            try:
                evolve_asset(asset, store, CONFIG["evolve_generations"], CONFIG["evolve_population"], send_alerts)
            except Exception:
                traceback.print_exc()
        meta["last_evolve"] = time.time()


def _scheduler():
    while True:
        run_trading()
        if time.time() - meta["last_evolve"] >= CONFIG["evolve_every_hours"] * 3600:
            run_evolution_job()
        time.sleep(CONFIG["check_every_minutes"] * 60)


if os.getenv("DISABLE_SCHEDULER") != "1":
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
    return {
        "mode": store.get_mode(),
        "modes": [
            {"key": "paper", "name": "تنفيذ ورقي (Paper)", "available": True,
             "note": "صفقات وهمية محاكاة محلياً، لا يُرسل أي أمر لأي منصة."},
            {"key": "demo", "name": "تجريبي (Binance Testnet)", "available": creds is not None,
             "note": ("أموال وهمية. للبيتكوين فقط (الذهب غير متاح على التجريبي)." if creds
                      else "أدخل مفاتيح التجربة أعلاه أولاً.")},
            {"key": "mt5", "name": "Exness / JustMarkets (MT5)", "available": False,
             "note": "يحتاج برنامج MetaTrader 5 على Windows، غير متاح على هذه الخدمة بعد."},
            {"key": "live", "name": "حقيقي (Live)", "available": False,
             "note": "غير مفعّل في هذه النسخة."},
        ],
        "keys": {"present": creds is not None,
                 "market": creds["market"] if creds else "spot",
                 "market_label": MARKETS[creds["market"]]["label"] if creds else None,
                 "masked": _mask(creds["api_key"]) if creds else None},
        "markets": [{"key": k, "label": v["label"]} for k, v in MARKETS.items()],
        "last_test": meta["last_test"],
    }


def _build_state() -> dict:
    balance = store.balance()
    start = CONFIG["start_balance"]
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
        "account": {"balance": balance, "start": start,
                    "return_pct": round((balance / start - 1) * 100, 2),
                    "trades": len(closed),
                    "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
                    "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None},
        "assets": assets,
        "closed": list(reversed(closed[-60:])),
        "equity": equity,
        "evolutions": store.recent_evolutions(12),
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
    elif mode == "demo":
        if store.get_broker_creds() is None:
            return jsonify({"error": "أدخل مفاتيح التجربة أولاً"}), 400
        store.set_mode("demo")
    else:
        return jsonify({"error": "هذا الوضع غير مفعّل في هذه النسخة"}), 400
    return jsonify({"ok": True, "note": f"تم التبديل إلى: {mode}"})


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


INDEX_HTML = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>مركز التحليل الذاتي</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans antialiased">
<div class="max-w-7xl mx-auto p-4 md:p-8 space-y-6">

  <header class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
    <div>
      <h1 class="text-2xl md:text-3xl font-bold bg-gradient-to-l from-amber-300 to-fuchsia-400 bg-clip-text text-transparent">مركز التحليل الذاتي</h1>
      <p class="text-slate-400 text-sm">تطوير استراتيجيات تلقائي · تنفيذ ورقي · إشعارات تليجرام</p>
    </div>
    <div class="flex flex-col sm:flex-row gap-2 items-stretch">
      <input id="pw" type="password" placeholder="كلمة المرور" class="bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-sm">
      <select id="scope" class="bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-sm">
        <option value="all">الذهب + البيتكوين</option>
        <option value="XAUUSD">الذهب فقط</option>
        <option value="BTCUSDT">البيتكوين فقط</option>
      </select>
      <button id="btnScope" class="bg-slate-800 hover:bg-slate-700 rounded-lg px-4 py-2 text-sm">حفظ الاختيار</button>
      <button id="btnRun" class="bg-slate-800 hover:bg-slate-700 rounded-lg px-4 py-2 text-sm">فحص الآن</button>
      <button id="btnEvolve" class="bg-gradient-to-l from-fuchsia-600 to-amber-500 text-slate-950 font-bold rounded-lg px-4 py-2 text-sm">🧬 تطوير الآن</button>
    </div>
  </header>
  <p id="msg" class="text-sm text-amber-200 min-h-[1.25rem]"></p>

  <!-- Broker settings -->
  <section class="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 space-y-4">
    <h2 class="font-bold">⚙️ ربط الوسيط (Broker Connection)</h2>
    <div class="grid gap-3 md:grid-cols-4">
      <label class="space-y-1"><span class="text-xs text-slate-400">نوع السوق</span>
        <select id="market" class="w-full bg-slate-800 rounded-lg p-2 text-sm"></select></label>
      <label class="space-y-1"><span class="text-xs text-slate-400">API Key</span>
        <input id="apiKey" type="text" autocomplete="off" spellcheck="false" class="w-full bg-slate-800 rounded-lg p-2 text-sm"></label>
      <label class="space-y-1"><span class="text-xs text-slate-400">API Secret</span>
        <input id="apiSecret" type="password" autocomplete="off" class="w-full bg-slate-800 rounded-lg p-2 text-sm"></label>
      <div class="flex items-end gap-2">
        <button id="btnSaveKeys" class="flex-1 bg-amber-400 hover:bg-amber-300 text-slate-900 font-bold rounded-lg p-2 text-sm">حفظ المفاتيح</button>
        <button id="btnClearKeys" class="bg-rose-900 hover:bg-rose-800 rounded-lg p-2 text-sm">حذف</button>
      </div>
    </div>
    <p id="keyStatus" class="text-sm text-slate-300"></p>
    <div id="modes" class="grid gap-3 md:grid-cols-3"></div>
    <div class="flex flex-wrap items-center gap-2">
      <button id="btnSetMode" class="bg-amber-400 hover:bg-amber-300 text-slate-900 font-bold rounded-lg px-4 py-2 text-sm">تطبيق الوضع المختار</button>
      <button id="btnTest" class="bg-slate-800 hover:bg-slate-700 rounded-lg px-4 py-2 text-sm">اختبار الاتصال بالمنصة التجريبية</button>
      <span id="testOut" class="text-sm text-slate-300"></span>
    </div>
    <div class="text-xs text-slate-400 leading-relaxed">
      المفاتيح تُحفظ على الخادم في قاعدة البيانات، ولا تُعاد عرضها كاملة بعد الحفظ. استخدم مفاتيح التجربة من testnet.binance.vision فقط.
    </div>
  </section>

  <!-- KPIs -->
  <section class="grid grid-cols-2 lg:grid-cols-4 gap-4" id="kpis"></section>

  <!-- Equity -->
  <section class="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 shadow-2xl shadow-fuchsia-950/20">
    <h2 class="font-bold mb-3">منحنى الرصيد الورقي</h2>
    <div class="relative h-64"><canvas id="equity"></canvas></div>
    <p id="equityEmpty" class="text-slate-500 text-sm text-center py-6 hidden">لا توجد صفقات مغلقة بعد.</p>
  </section>

  <!-- Assets -->
  <section id="assets" class="grid gap-4 lg:grid-cols-2"></section>

  <!-- Open + closed trades -->
  <section class="grid gap-4 lg:grid-cols-2">
    <div class="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
      <h2 class="font-bold mb-3">الصفقات المفتوحة</h2>
      <div class="overflow-x-auto"><table class="w-full text-sm">
        <thead class="text-slate-400"><tr>
          <th class="text-right p-2">الأصل</th><th class="text-right p-2">الاتجاه</th>
          <th class="text-right p-2">الدخول</th><th class="text-right p-2">الوقف</th><th class="text-right p-2">الهدف</th>
        </tr></thead>
        <tbody id="openRows"></tbody>
      </table></div>
    </div>
    <div class="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
      <h2 class="font-bold mb-3">الصفقات المغلقة</h2>
      <div class="overflow-x-auto max-h-96"><table class="w-full text-sm">
        <thead class="text-slate-400 sticky top-0 bg-slate-900"><tr>
          <th class="text-right p-2">الأصل</th><th class="text-right p-2">الاتجاه</th>
          <th class="text-right p-2">النتيجة R</th><th class="text-right p-2">الربح/الخسارة</th><th class="text-right p-2">الإغلاق</th>
        </tr></thead>
        <tbody id="closedRows"></tbody>
      </table></div>
    </div>
  </section>

  <!-- Leaderboard + evolution log -->
  <section class="grid gap-4 lg:grid-cols-3">
    <div class="lg:col-span-2 bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
      <h2 class="font-bold mb-3">لوحة المتصدرين (استراتيجيات ناجحة في الاختبار)</h2>
      <div class="overflow-x-auto"><table class="w-full text-sm">
        <thead class="text-slate-400"><tr>
          <th class="text-right p-2">الأصل</th><th class="text-right p-2">القواعد</th>
          <th class="text-right p-2">نجاح%</th><th class="text-right p-2">PF</th>
          <th class="text-right p-2">أقصى تراجع R</th><th class="text-right p-2">الدرجة</th>
        </tr></thead>
        <tbody id="boardRows"></tbody>
      </table></div>
    </div>
    <div class="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
      <h2 class="font-bold mb-3">سجل التطوير</h2>
      <ul id="evoLog" class="space-y-2 text-sm max-h-96 overflow-y-auto"></ul>
    </div>
  </section>

  <footer class="text-xs text-slate-500 leading-relaxed">
    ⚠️ هذا النظام تعليمي ويعمل بتنفيذ ورقي وهمي. نتائج الاختبار التاريخي لا تضمن أرباحاً مستقبلية، والبحث التطوري قد يُفرط في التخصيص للبيانات السابقة رغم التحقق خارج العينة.
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, d = 2) => (v == null || isNaN(v)) ? "—" : Number(v).toLocaleString("en-US", {maximumFractionDigits: d, minimumFractionDigits: d});
const dt = (s) => s ? String(s).slice(0, 16).replace("T", " ") : "—";
const pnlClass = (v) => (v > 0 ? "text-emerald-400" : v < 0 ? "text-rose-400" : "text-slate-300");
const sideTxt = (s) => (s === "buy" ? '<span class="text-emerald-400">شراء</span>' : '<span class="text-rose-400">بيع</span>');
const describe = (g) => g ? `ATR ${g.atr_len} · عمق ${g.depth_mult} · بفر ${g.sl_buffer} · ${g.target_r}R · ${g.tf}H · EMA200 ${g.trend ? "✔" : "✘"}` : "";

let chart = null;
let lastOk = 0;

function msg(t) { $("msg").textContent = t; }

async function load() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
    lastOk = Date.now();
  } catch (e) {
    msg("تعذّر تحديث البيانات، إعادة المحاولة تلقائياً...");
  }
}

function render(s) {
  const a = s.account;
  $("kpis").innerHTML = [
    ["الرصيد الورقي", `$${num(a.balance)}`, `البداية $${num(a.start, 0)}`],
    ["العائد", `<span class="${pnlClass(a.return_pct)}">${num(a.return_pct)}%</span>`, `${a.trades} صفقة مغلقة`],
    ["نسبة النجاح", `${num(a.win_rate, 1)}%`, "من الصفقات المغلقة"],
    ["معامل الربح", num(a.profit_factor), "إجمالي الأرباح ÷ الخسائر"],
  ].map(([t, v, sub]) => `
    <div class="bg-slate-900/60 border border-slate-800 rounded-2xl p-4">
      <div class="text-xs text-slate-400">${t}</div>
      <div class="text-2xl font-bold mt-1 tabular-nums">${v}</div>
      <div class="text-xs text-slate-500 mt-1">${sub}</div>
    </div>`).join("");

  drawEquity(s.equity);
  renderSettings(s.settings);

  $("assets").innerHTML = Object.entries(s.assets).map(([k, v]) => assetCard(k, v)).join("");

  const openAll = Object.values(s.assets).flatMap(v => v.open || []);
  $("openRows").innerHTML = openAll.length ? openAll.map(t => `
    <tr class="border-t border-slate-800">
      <td class="p-2">${esc(t.asset)}</td><td class="p-2">${sideTxt(t.side)}</td>
      <td class="p-2">${num(t.entry)}</td><td class="p-2">${num(t.stop)}</td><td class="p-2">${num(t.target)}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="p-3 text-slate-500">لا توجد صفقات مفتوحة</td></tr>`;

  $("closedRows").innerHTML = s.closed.length ? s.closed.map(t => `
    <tr class="border-t border-slate-800">
      <td class="p-2">${esc(t.asset)}</td><td class="p-2">${sideTxt(t.side)}</td>
      <td class="p-2 ${pnlClass(t.r)}">${num(t.r)}R</td>
      <td class="p-2 ${pnlClass(t.pnl_usd)}">${t.pnl_usd > 0 ? "+" : ""}${num(t.pnl_usd)}$</td>
      <td class="p-2 text-slate-400">${dt(t.closed_utc)}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="p-3 text-slate-500">لا توجد صفقات مغلقة بعد</td></tr>`;

  const board = Object.entries(s.assets).flatMap(([k, v]) => v.leaderboard.map(x => ({...x, asset: v.label})));
  $("boardRows").innerHTML = board.length ? board.map(x => `
    <tr class="border-t border-slate-800">
      <td class="p-2 text-xs">${esc(x.asset)}</td>
      <td class="p-2 text-xs text-slate-300">${esc(describe(x.genome))}</td>
      <td class="p-2">${num(x.result.val.win_rate, 1)}</td>
      <td class="p-2">${num(x.result.val.profit_factor)}</td>
      <td class="p-2">${num(x.result.val.max_dd_r)}</td>
      <td class="p-2 text-amber-300">${num(x.score, 3)}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="p-3 text-slate-500">لم تُعتمد أي استراتيجية بعد. اضغط "تطوير الآن".</td></tr>`;

  $("evoLog").innerHTML = s.evolutions.length ? s.evolutions.map(e => `
    <li class="bg-slate-950/60 border border-slate-800 rounded-lg p-3">
      <div class="flex justify-between text-xs text-slate-400"><span>${esc(e.asset)}</span><span>${dt(e.created_utc)}</span></div>
      <div class="mt-1">جُرّب ${e.evaluated} · نجح ${e.accepted}</div>
      <div class="text-xs text-slate-400">${esc(e.note)}</div>
    </li>`).join("") : `<li class="text-slate-500">لا يوجد سجل بعد</li>`;

  msg(`آخر تحديث: ${new Date().toLocaleTimeString("ar-SA")}`);
}

function assetCard(key, v) {
  const L = (v.live && v.live.latest) || {};
  const champ = v.champion;
  const err = v.live && v.live.error ? `<p class="text-rose-400 text-xs">خطأ: ${esc(v.live.error)}</p>` : "";
  const champBlock = champ ? `
    <div class="bg-slate-950/60 rounded-lg p-3 text-xs space-y-1">
      <div class="text-amber-300 font-semibold">الاستراتيجية المعتمدة</div>
      <div class="text-slate-300">${esc(describe(champ.genome))}</div>
      <div class="text-slate-400">تحقق: نجاح ${num(champ.result.val.win_rate,1)}% · PF ${num(champ.result.val.profit_factor)} · تراجع ${num(champ.result.val.max_dd_r)}R · ${champ.result.val.trades} صفقة</div>
      <div class="text-slate-500">منذ ${dt(champ.since)}</div>
    </div>` : `<div class="bg-rose-950/40 border border-rose-900 rounded-lg p-3 text-xs text-rose-300">لا توجد استراتيجية معتمدة، التداول متوقف لهذا الأصل حتى يجد التطوير قاعدة ناجحة.</div>`;
  return `
  <div class="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 space-y-3">
    <div class="flex items-start justify-between">
      <div>
        <h3 class="font-bold text-lg text-amber-300">${esc(v.label)}</h3>
        <span class="text-xs ${v.session_open ? "text-emerald-400" : "text-rose-400"}">${v.session_open ? "🟢 السوق مفتوح" : "🔴 السوق مغلق (عطلة)"}</span>
      </div>
      <span class="tabular-nums text-xl font-bold">${num(v.live && v.live.price)}</span>
    </div>
    ${err}
    ${champBlock}
    <div class="grid grid-cols-2 gap-2 text-xs text-slate-300">
      <div>قمة الأمس: ${num(L.prev_day_high)}</div>
      <div>قاع الأمس: ${num(L.prev_day_low)}</div>
      <div class="col-span-2 text-amber-200">بانتظار التأكيد: ${esc(L.pending || "—")}</div>
      <div class="col-span-2 text-slate-500">صفقات مفتوحة: ${(v.open || []).length}</div>
    </div>
  </div>`;
}

function drawEquity(points) {
  const empty = points.length === 0;
  $("equityEmpty").classList.toggle("hidden", !empty);
  if (empty) { if (chart) { chart.destroy(); chart = null; } return; }
  const labels = points.map(p => dt(p.t));
  const data = points.map(p => p.bal);
  if (!chart) {
    chart = new Chart($("equity"), {
      type: "line",
      data: { labels, datasets: [{ label: "الرصيد", data, borderColor: "#f0abfc", backgroundColor: "rgba(240,171,252,0.12)",
        fill: true, tension: 0.3, pointRadius: 2 }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { ticks: { color: "#64748b", maxTicksLimit: 8 }, grid: { color: "#1e293b" } },
                  y: { ticks: { color: "#64748b" }, grid: { color: "#1e293b" } } } },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = data;
    chart.update();
  }
}

async function act(url, body = {}) {
  msg("جارٍ الإرسال...");
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Admin-Password": $("pw").value },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    msg(r.ok ? (j.note || "تم ✔") : "خطأ: " + (j.error || r.status));
    setTimeout(load, 3000);
  } catch (e) { msg("تعذّر الإرسال، تحقق من الاتصال."); }
}

let pickedMode = null;

async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Admin-Password": $("pw").value },
    body: JSON.stringify(body || {}),
  });
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok, data: j, status: r.status };
}

$("btnSaveKeys").onclick = async () => {
  const res = await postJson("/api/keys", {
    api_key: $("apiKey").value, api_secret: $("apiSecret").value, market: $("market").value });
  msg(res.ok ? res.data.note : "خطأ: " + (res.data.error || res.status));
  if (res.ok) { $("apiSecret").value = ""; $("apiKey").value = ""; }
  setTimeout(load, 800);
};
$("btnClearKeys").onclick = async () => {
  if (!confirm("حذف المفاتيح؟")) return;
  const res = await postJson("/api/keys/clear");
  msg(res.ok ? res.data.note : "خطأ: " + (res.data.error || res.status));
  setTimeout(load, 800);
};

function renderSettings(st) {
  if (!st) return;
  const current = pickedMode || st.mode;
  $("modes").innerHTML = st.modes.map(m => `
    <label class="block rounded-xl border p-4 ${m.available ? "border-slate-700 cursor-pointer hover:border-amber-400" : "border-slate-800 opacity-50"}">
      <div class="flex items-center gap-2">
        <input type="radio" name="mode" value="${esc(m.key)}" ${m.available ? "" : "disabled"} ${current === m.key ? "checked" : ""}>
        <span class="font-semibold">${esc(m.name)}</span>
        ${st.mode === m.key ? '<span class="text-xs bg-emerald-900 text-emerald-300 rounded px-2 py-0.5">مفعل الآن</span>' : ""}
      </div>
      <p class="text-xs text-slate-400 mt-2">${esc(m.note)}</p>
    </label>`).join("");
  document.querySelectorAll('input[name="mode"]').forEach(el =>
    el.addEventListener("change", () => { pickedMode = el.value; }));
  const sel = $("market");
  if (!sel.options.length) sel.innerHTML = st.markets.map(m => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
  $("keyStatus").textContent = st.keys.present
    ? `🔑 مفاتيح محفوظة (${st.keys.market_label}) · ${st.keys.masked}`
    : "لا توجد مفاتيح محفوظة بعد.";
  if (st.last_test) {
    $("testOut").textContent = st.last_test.ok
      ? `✔ آخر اختبار ناجح: ${num(st.last_test.usdt_free)} USDT`
      : `✘ آخر اختبار فشل: ${st.last_test.error}`;
  }
}

$("btnSetMode").onclick = () => {
  const sel = document.querySelector('input[name="mode"]:checked');
  if (!sel) return msg("اختر وضعاً أولاً");
  act("/api/settings", { mode: sel.value }).then(() => { pickedMode = null; });
};
$("btnTest").onclick = async () => {
  $("testOut").textContent = "جارٍ الاختبار...";
  try {
    const res = await postJson("/api/broker/test");
    $("testOut").textContent = res.ok ? "✔ " + res.data.note : "✘ " + (res.data.error || res.status);
  } catch (e) { $("testOut").textContent = "تعذّر الاتصال"; }
};

$("btnScope").onclick = () => act("/api/config", { assets: $("scope").value });
$("btnRun").onclick = () => act("/api/run");
$("btnEvolve").onclick = () => act("/api/evolve");

load();
setInterval(load, 30000);
</script>
</body>
</html>
'''
