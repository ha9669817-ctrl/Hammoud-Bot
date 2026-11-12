// لوحة مبسّطة: الرصيد، إجمالي ربح البوتات، وقائمة البوتات مع تشغيل/إيقاف لكل أصل.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const ASSET_NAME = { XAUUSD: "الذهب", BTCUSDT: "البيتكوين" };
  const PW_KEY = "ma_admin_pw";

  try { $("pw").value = localStorage.getItem(PW_KEY) || ""; } catch (e) {}
  $("pw").addEventListener("input", () => {
    try { localStorage.setItem(PW_KEY, $("pw").value); } catch (e) {}
  });

  const fmt = (n) => (n == null || isNaN(n)) ? "—" :
    Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const signed = (n) => (n > 0 ? "+" : "") + fmt(n);

  function msg(text, kind) {
    const el = $("msg");
    el.textContent = text || "";
    el.className = kind || "";
  }

  async function getState() {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 8000);
    try {
      const r = await fetch("/api/state", { cache: "no-store", signal: ctl.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function renderAccount(s) {
    const acc = s.account || {};
    $("balance").textContent = acc.balance == null ? "—" : fmt(acc.balance) + " $";
    if (acc.source === "tickerall") {
      $("balanceSub").textContent = acc.equity == null ? "حساب تجريبي · من الوسيط"
        : "حساب تجريبي · الأصول الحالية " + fmt(acc.equity) + " $";
    } else {
      $("balanceSub").textContent = "بانتظار اتصال الوسيط";
    }
  }

  function renderTotals(board) {
    const total = board.total_profit || 0;
    const el = $("totalProfit");
    el.textContent = signed(total) + " $";
    el.className = "big " + (total > 0 ? "up" : total < 0 ? "down" : "");
    const closed = (board.bots || []).reduce((a, b) => a + b.trades, 0);
    $("totalSub").textContent = closed + " صفقة مغلقة";
  }

  function renderBots(s) {
    const board = s.board || { bots: [], blocked: {} };
    const assets = (s.config && s.config.assets) || Object.keys(ASSET_NAME);
    const list = $("botList");
    list.replaceChildren();
    for (const b of board.bots) {
      const row = document.createElement("div");
      row.className = "bot";

      const info = document.createElement("div");
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = b.name;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${b.trades} صفقة · ربح ${signed(b.profit)}$`;
      info.append(name, meta);

      const btns = document.createElement("div");
      btns.className = "btns";
      for (const a of assets) {
        const running = b.running.includes(a);
        const blockedWhy = board.blocked && board.blocked[a];
        const btn = document.createElement("button");
        btn.dataset.asset = a;
        btn.dataset.bot = b.id;
        btn.dataset.stop = running ? "1" : "0";
        if (running) {
          btn.textContent = "إيقاف · " + ASSET_NAME[a];
          btn.className = "stop";
        } else {
          btn.textContent = "تشغيل · " + ASSET_NAME[a];
          btn.className = "on";
          if (blockedWhy) {
            btn.disabled = true;
            btn.title = blockedWhy;
            btn.textContent = ASSET_NAME[a] + " محظور";
          }
        }
        btns.append(btn);
      }
      row.append(info, btns);
      list.append(row);
    }
  }

  async function refresh() {
    try {
      const s = await getState();
      renderAccount(s);
      renderTotals(s.board || { bots: [] });
      renderBots(s);
      $("liveDot").className = "dot ok";
      $("liveText").textContent = "متصل · " + new Date().toLocaleTimeString("ar-SA");
    } catch (e) {
      $("liveDot").className = "dot bad";
      $("liveText").textContent = "تعذّر تحديث البيانات، سيُعاد المحاولة";
    }
  }

  $("botList").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-asset]");
    if (!btn || btn.disabled) return;
    const pwd = $("pw").value.trim();
    if (!pwd) {
      msg("أدخل كلمة مرور الإدارة أولاً", "err");
      $("pw").focus();
      return;
    }
    const stop = btn.dataset.stop === "1";
    const body = stop ? { asset: btn.dataset.asset } : { asset: btn.dataset.asset, bot: btn.dataset.bot };
    btn.disabled = true;
    try {
      const r = await fetch(stop ? "/api/bot/stop" : "/api/bot/start", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Admin-Password": pwd },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || "فشل الطلب (" + r.status + ")");
      msg(data.note || "تم", "ok");
    } catch (err) {
      msg(err.message, "err");
    } finally {
      btn.disabled = false;
      await refresh();
    }
  });

  refresh();
  setInterval(refresh, 5000);
})();
