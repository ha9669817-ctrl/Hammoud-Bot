"""تخزين دائم لسجل البوت في مستودع GitHub خاص (قرص Render المجاني يُمسح عند كل إعادة تشغيل).

يتطلب متغيري بيئة: PERSIST_REPO («مالك/اسم» لمستودع خاص مخصص للبيانات فقط) وGITHUB_TOKEN (توكن بصلاحية
Contents: Read and write على ذلك المستودع وحده). بدونهما يبقى معطلاً ولا يؤثر على البوت.
لا نخزّن في المستودع أي سر: فقط الأحداث والصفقات والدروس وحالة البوت وزر التشغيل.
"""
import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import requests

API = "https://api.github.com"
FILES = ["events.jsonl", "trades.jsonl", "lessons.md", "state.json"]
META_KEYS = ["signalbot_on", "ta_modify_variant", "analyst_on"]
SYNC_EVERY_S = 60
MAX_LOCAL_BYTES = 2_000_000


def _canon(name: str, data: bytes) -> str:
    """بصمة المحتوى. في state.json نتجاهل الحقول كثيرة التغير حتى لا نرسل كل ثانيتين."""
    if name == "state.json":
        try:
            d = json.loads(data)
            d.pop("last_bar_ts", None)
            for v in (d.get("adopt") or {}).values():
                v.pop("best", None)
                v.pop("since", None)
            for v in (d.get("trades") or {}).values():
                v.pop("best", None)
            data = json.dumps(d, sort_keys=True).encode()
        except ValueError:
            pass
    return hashlib.sha256(data).hexdigest()


class Persist:
    def __init__(self, token, repo, branch, folder: Path, store, http=requests):
        self.token, self.repo, self.branch = token, repo, branch
        self.dir, self.store, self.http = Path(folder), store, http
        self.sha = {}          # مسار ← sha في GitHub
        self.last_hash = {}    # مسار ← بصمة آخر ما أرسلناه
        self.last_push = 0.0
        self.state = {"enabled": True, "error": None, "last_sync": None, "restored": 0}

    @classmethod
    def from_env(cls, folder, store):
        token = (os.getenv("GITHUB_TOKEN") or "").strip()
        repo = (os.getenv("PERSIST_REPO") or "").strip()
        if not token or "/" not in repo:
            p = cls("", "", "", folder, store)
            p.state.update(enabled=False, error="غير مضبوط: أضف PERSIST_REPO وGITHUB_TOKEN في Render")
            return p
        return cls(token, repo, (os.getenv("PERSIST_BRANCH") or "bot-data").strip(), folder, store)

    # ---------- GitHub ----------
    def _h(self, raw=False):
        return {"Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28",
                "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}

    def _err(self, where, r):
        # لا نضع التوكن ولا الرد الكامل في الرسالة
        return f"{where}: HTTP {r.status_code}"

    def _ensure_branch(self):
        r = self.http.get(f"{API}/repos/{self.repo}/git/ref/heads/{self.branch}", headers=self._h(), timeout=20)
        if r.status_code == 200:
            return
        if r.status_code != 404:
            raise RuntimeError(self._err("فحص الفرع", r))
        repo = self.http.get(f"{API}/repos/{self.repo}", headers=self._h(), timeout=20)
        if repo.status_code != 200:
            raise RuntimeError(self._err("قراءة المستودع", repo))
        info = repo.json()
        if not info.get("private"):
            raise RuntimeError("المستودع عام! استخدم مستودعاً خاصاً لحماية سجل صفقاتك")
        base = self.http.get(f"{API}/repos/{self.repo}/git/ref/heads/{info.get('default_branch', 'main')}",
                             headers=self._h(), timeout=20)
        if base.status_code != 200:
            raise RuntimeError(self._err("قراءة الفرع الأساسي (المستودع فارغ؟ أضف README)", base))
        c = self.http.post(f"{API}/repos/{self.repo}/git/refs", headers=self._h(), timeout=20,
                           json={"ref": f"refs/heads/{self.branch}", "sha": base.json()["object"]["sha"]})
        if c.status_code not in (200, 201):
            raise RuntimeError(self._err("إنشاء الفرع", c))

    def _get_file(self, path):
        r = self.http.get(f"{API}/repos/{self.repo}/contents/{path}", params={"ref": self.branch},
                          headers=self._h(raw=True), timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(self._err("قراءة " + path, r))
        meta = self.http.get(f"{API}/repos/{self.repo}/contents/{path}", params={"ref": self.branch},
                             headers=self._h(), timeout=30)
        if meta.status_code == 200:
            self.sha[path] = meta.json().get("sha")
        return r.content

    def _put_file(self, path, data: bytes):
        body = {"message": "data sync", "branch": self.branch,
                "content": base64.b64encode(data).decode()}
        if self.sha.get(path):
            body["sha"] = self.sha[path]
        r = self.http.put(f"{API}/repos/{self.repo}/contents/{path}", headers=self._h(), json=body, timeout=30)
        if r.status_code == 409 or r.status_code == 422:   # sha قديم: نجلب الحالي ونعيد مرة واحدة
            m = self.http.get(f"{API}/repos/{self.repo}/contents/{path}", params={"ref": self.branch},
                              headers=self._h(), timeout=30)
            if m.status_code == 200:
                body["sha"] = m.json().get("sha")
                r = self.http.put(f"{API}/repos/{self.repo}/contents/{path}", headers=self._h(), json=body, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(self._err("رفع " + path, r))
        self.sha[path] = (r.json().get("content") or {}).get("sha", self.sha.get(path))

    # ---------- استعادة ومزامنة ----------
    def restore(self):
        """عند قرص فارغ فقط (لا state.json محلي): ننزّل آخر نسخة محفوظة. لا نكتب فوق بيانات محلية."""
        if not self.state["enabled"]:
            return 0
        try:
            self._ensure_branch()
            self.dir.mkdir(parents=True, exist_ok=True)
            n = 0
            if not (self.dir / "state.json").exists():
                for name in FILES:
                    data = self._get_file("data/" + name)
                    if data is not None:
                        (self.dir / name).write_bytes(data)
                        self.last_hash[name] = _canon(name, data)
                        n += 1
                meta = self._get_file("data/meta.json")
                if meta:
                    for k, v in json.loads(meta).items():
                        if k in META_KEYS and self.store.meta_get(k) is None:
                            self.store.meta_set(k, v)
                    self.last_hash["meta.json"] = _canon("meta.json", meta)
                    n += 1
            self.state.update(restored=n, error=None)
            return n
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {str(e)[:140]}"
            return 0

    def _meta_bytes(self):
        d = {k: self.store.meta_get(k) for k in META_KEYS if self.store.meta_get(k) is not None}
        return json.dumps(d, sort_keys=True).encode()

    def sync(self, force=False):
        """يرفع الملفات المتغيرة فقط، وبحد أدنى 60 ثانية بين الدفعات."""
        if not self.state["enabled"]:
            return 0
        if not force and time.time() - self.last_push < SYNC_EVERY_S:
            return 0
        pushed = 0
        try:
            self._ensure_branch()
            items = []
            for name in FILES:
                p = self.dir / name
                if p.exists():
                    if p.stat().st_size > MAX_LOCAL_BYTES and name.endswith(".jsonl"):
                        lines = p.read_bytes().splitlines()[-3000:]
                        p.write_bytes(b"\n".join(lines) + b"\n")
                    items.append((name, p.read_bytes()))
            items.append(("meta.json", self._meta_bytes()))
            for name, data in items:
                h = _canon(name, data)
                if self.last_hash.get(name) == h:
                    continue
                self._put_file("data/" + name, data)
                self.last_hash[name] = h
                pushed += 1
            self.last_push = time.time()
            self.state.update(error=None, last_sync=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {str(e)[:140]}"
            self.last_push = time.time()   # لا نكرر الطلب كل ثانية عند الفشل
        return pushed

    def start(self):
        if not self.state["enabled"]:
            return

        def loop():
            while True:
                time.sleep(15)
                self.sync()
        threading.Thread(target=loop, daemon=True).start()
