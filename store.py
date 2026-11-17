"""مخزن مفتاح/قيمة صغير (SQLite) لحالة الموقع."""
import json
import os
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.path = path
        with self._tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @contextmanager
    def _tx(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def meta_get(self, k, default=None):
        with self._tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def meta_set(self, k, v):
        with self._tx() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))
