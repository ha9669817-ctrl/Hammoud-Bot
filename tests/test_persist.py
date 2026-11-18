import base64
import json

from persist import Persist, _canon
from store import Store


class Resp:
    def __init__(self, code, body=None, content=b""):
        self.status_code, self._b, self.content = code, body or {}, content

    def json(self):
        return self._b


class FakeGitHub:
    """يحاكي واجهة GitHub لمستودع واحد: فرع bot-data وملفات contents."""
    def __init__(self, private=True, branch=True):
        self.private, self.branch = private, branch
        self.files, self.puts, self.n = {}, [], 0

    def get(self, url, params=None, headers=None, timeout=None):
        if "/git/ref/heads/bot-data" in url:
            return Resp(200 if self.branch else 404, {"object": {"sha": "x"}})
        if "/git/ref/heads/main" in url:
            return Resp(200, {"object": {"sha": "base"}})
        if "/contents/" in url:
            path = url.split("/contents/")[1]
            if path not in self.files:
                return Resp(404)
            data, sha = self.files[path]
            if (headers or {}).get("Accept", "").startswith("application/vnd.github.raw"):
                return Resp(200, content=data)
            return Resp(200, {"sha": sha})
        return Resp(200, {"private": self.private, "default_branch": "main"})

    def post(self, url, headers=None, json=None, timeout=None):
        self.branch = True
        return Resp(201)

    def put(self, url, headers=None, json=None, timeout=None):
        path = url.split("/contents/")[1]
        self.n += 1
        self.files[path] = (base64.b64decode(json["content"]), f"sha{self.n}")
        self.puts.append(path)
        return Resp(200, {"content": {"sha": f"sha{self.n}"}})


def make(tmp_path, gh, name="a"):
    store = Store(str(tmp_path / f"{name}.db"))
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return Persist("tok", "me/data", "bot-data", folder, store, http=gh), store, folder


def test_sync_pushes_only_changed_and_throttles(tmp_path):
    gh = FakeGitHub()
    p, store, folder = make(tmp_path, gh)
    (folder / "events.jsonl").write_text('{"a":1}\n')
    (folder / "state.json").write_text(json.dumps({"trades": {}, "last_bar_ts": 1}))
    store.meta_set("signalbot_on", True)
    assert p.sync(force=True) == 3                     # events + state + meta
    assert p.sync(force=True) == 0                     # لا تغيير
    (folder / "state.json").write_text(json.dumps({"trades": {}, "last_bar_ts": 2}))
    assert p.sync(force=True) == 0                     # last_bar_ts وحده لا يستحق إرسالاً
    (folder / "events.jsonl").write_text('{"a":1}\n{"a":2}\n')
    p.last_push = 1e18                                 # داخل نافذة 60 ثانية
    assert p.sync() == 0
    assert p.sync(force=True) == 1


def test_restore_on_empty_disk_then_not_over_existing(tmp_path):
    gh = FakeGitHub()
    p, store, folder = make(tmp_path, gh, "old")
    (folder / "events.jsonl").write_text('{"e":1}\n')
    (folder / "trades.jsonl").write_text('{"t":1}\n')
    (folder / "state.json").write_text(json.dumps({"paused": {"X": "r"}}))
    store.meta_set("signalbot_on", True)
    p.sync(force=True)
    # قرص جديد فارغ (إعادة تشغيل Render)
    p2, store2, folder2 = make(tmp_path, gh, "new")
    assert p2.restore() >= 4
    assert json.loads((folder2 / "state.json").read_text())["paused"] == {"X": "r"}
    assert (folder2 / "trades.jsonl").read_text() == '{"t":1}\n'
    assert store2.meta_get("signalbot_on") is True
    # قرص فيه بيانات محلية: لا نكتب فوقها
    (folder2 / "events.jsonl").write_text('{"local":1}\n')
    p2.restore()
    assert (folder2 / "events.jsonl").read_text() == '{"local":1}\n'


def test_refuses_public_repo_and_hides_token(tmp_path):
    gh = FakeGitHub(private=False, branch=False)
    p, _, folder = make(tmp_path, gh)
    (folder / "events.jsonl").write_text("x\n")
    assert p.sync(force=True) == 0 and "عام" in p.state["error"]
    assert "tok" not in json.dumps(p.state)
    assert not gh.files


def test_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PERSIST_REPO", raising=False)
    p = Persist.from_env(tmp_path / "z", Store(str(tmp_path / "z.db")))
    assert p.state["enabled"] is False and p.sync(force=True) == 0 and p.restore() == 0


def test_canon_ignores_noise_only_for_state():
    a = json.dumps({"adopt": {"1": {"best": 1, "since": 1, "stop": 5}}, "last_bar_ts": 1}).encode()
    b = json.dumps({"adopt": {"1": {"best": 9, "since": 9, "stop": 5}}, "last_bar_ts": 9}).encode()
    c = json.dumps({"adopt": {"1": {"best": 9, "since": 9, "stop": 6}}}).encode()
    assert _canon("state.json", a) == _canon("state.json", b) != _canon("state.json", c)
    assert _canon("events.jsonl", a) != _canon("events.jsonl", b)
