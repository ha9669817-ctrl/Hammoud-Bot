"""سجل دائم: الصفقات، الأحداث، الدروس، والحالة. كل شيء ملفات نصية قابلة للقراءة."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, directory: str | None = None):
        self.dir = Path(directory or os.getenv("SMARTBOT_DIR", "smartbot_data"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.dir / "trades.jsonl"
        self.events_path = self.dir / "events.jsonl"
        self.lessons_path = self.dir / "lessons.md"
        self.state_path = self.dir / "state.json"

    def _append(self, path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def trade(self, rec: dict) -> None:
        self._append(self.trades_path, json.dumps({"at": _now(), **rec}, ensure_ascii=False))

    def event(self, kind: str, msg: str, data: dict | None = None) -> None:
        rec = {"at": _now(), "kind": kind, "msg": msg, "data": data or {}}
        self._append(self.events_path, json.dumps(rec, ensure_ascii=False))

    def lesson(self, text: str) -> None:
        with open(self.lessons_path, "a", encoding="utf-8") as fh:
            fh.write(f"- [{_now()}] {text}\n")
        self.event("lesson", text)

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path, limit: int = 50) -> list:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines[-limit:] if x.strip()]

    def lessons_tail(self, limit: int = 15) -> list:
        if not self.lessons_path.exists():
            return []
        return self.lessons_path.read_text(encoding="utf-8").splitlines()[-limit:]
