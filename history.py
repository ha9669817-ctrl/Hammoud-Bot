"""سحب الشموع التاريخية من الوسيط إلى ملفات CSV."""
import csv
import json
import re
import threading
import time
from pathlib import Path

import pandas as pd

from broker import INTERVAL_SEC, _items, _ts_any, utc_now

HIST_TF = {"5m": "M5", "15m": "M15", "1h": "H1", "1d": "D1", "1w": "W1", "1mo": "MN1"}
HIST_CHUNK = 500
HIST_STALE_S = 1800
HIST_LOCK = threading.Lock()
HIST = {"running": False, "started": None, "finished": None, "jobs": {}, "gen": 0, "t0": 0.0}


def _hist_name(symbol: str, interval: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", symbol)
    return f"{safe}_{interval}.csv"


def fetch_candles_span(client, symbol: str, interval: str, start, end, sink, alive=lambda: True) -> int:
    """يسحب شموع interval بين start و end على دفعات. إذا أعاد الوسيط أقل من المطلوب
    يكمل من آخر شمعة وصلت، فلا تضيع شموع بسبب سقف الخادم."""
    tf = HIST_TF[interval]
    step = pd.Timedelta(seconds=INTERVAL_SEC[interval])
    cur, seen, total = start, set(), 0
    while cur < end:
        if not alive():
            raise RuntimeError("أُلغي السحب لأن سحباً أحدث بدأ")
        win_end = min(end, cur + step * HIST_CHUNK)
        data = client._acct("GET", "/candles", params={
            "symbol": symbol, "timeframe": tf, "count": HIST_CHUNK,
            "from": cur.isoformat(), "to": win_end.isoformat()})
        rows = []
        for c in _items(data, "candles", "data", "items", "bars") or []:
            if not isinstance(c, dict) or c.get("open") is None:
                continue
            t = _ts_any(c.get("time") or c.get("timestamp") or c.get("open_time") or c.get("t"))
            if t is None:
                continue
            t = pd.to_datetime(t, utc=True)
            rows.append((t, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                         float(c.get("tick_volume") or c.get("volume") or 0)))
        fresh = []
        for r in rows:
            if r[0] not in seen and cur <= r[0] < end:
                seen.add(r[0])
                fresh.append(r)
        if fresh:
            sink(fresh)  # يُكتب إلى الملف مباشرة، فلا تتراكم الذاكرة
            total += len(fresh)
        seen = {r for r in seen if r >= cur - step * 2} if len(seen) > 5000 else seen
        if rows:
            # إن لم تصل الشموع إلى نهاية النافذة (سقف الخادم أو فجوة إغلاق السوق) نكمل من آخر شمعة.
            # التقدم مضمون: المؤشر لا يرجع أبداً حتى لو أعاد الخادم شموعاً أقدم.
            last = max(r[0] for r in rows)
            cur = max(last + step, cur + step)
        else:
            cur = win_end
        time.sleep(0.2)  # تهدئة لتفادي تجاوز حدود الوسيط
    return total


def run_history_job(client, folder: Path, symbols, intervals, days: int, gen: int) -> None:
    def alive():
        return HIST["gen"] == gen
    end = pd.Timestamp.now(tz="UTC").floor("min")
    start = end - pd.Timedelta(days=days)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for sym in symbols:
            for itv in intervals:
                if not alive():
                    return
                key = f"{sym}|{itv}"
                with HIST_LOCK:
                    HIST["jobs"][key] = {"status": "running", "rows": 0}
                try:
                    with (folder / _hist_name(sym, itv)).open("w", encoding="utf-8", newline="") as fh:
                        writer = csv.writer(fh)
                        writer.writerow(["open_time", "open", "high", "low", "close", "volume"])

                        def sink(batch):
                            for t, o, h, l, c, v in batch:
                                writer.writerow([str(t), o, h, l, c, v])

                        n = fetch_candles_span(client, sym, itv, start, end, sink, alive)
                    status = {"status": "done", "rows": n}
                except Exception as exc:
                    status = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                with HIST_LOCK:
                    HIST["jobs"][key] = {**HIST["jobs"].get(key, {}), **status}
    finally:
        with HIST_LOCK:
            if alive():
                HIST["running"] = False
                HIST["finished"] = utc_now()


def begin(client, folder, symbols, intervals, days):
    """يبدأ سحباً جديداً. يعيد False إن كان هناك سحب حيّ."""
    with HIST_LOCK:
        if HIST["running"] and time.time() - HIST.get("t0", 0.0) < HIST_STALE_S:
            return False
        gen = HIST["gen"] + 1
        HIST.update({"running": True, "started": utc_now(), "finished": None, "jobs": {},
                     "gen": gen, "t0": time.time()})
    threading.Thread(target=run_history_job, args=(client, Path(folder), symbols, intervals, days, gen),
                     daemon=True).start()
    return True


def snapshot(folder):
    with HIST_LOCK:
        snap = json.loads(json.dumps(HIST, default=str))
    folder = Path(folder)
    snap["files"] = sorted(p.name for p in folder.glob("*.csv")) if folder.exists() else []
    return snap
