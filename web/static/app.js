// لوحة مبسّطة: حالة الوسيط، الرصيد، إجمالي ربح البوتات، وقائمة البوتات.
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

  // يشرح للمستخدم لماذا الوسيط غير متصل، بدون أي رقم وهمي.
  function renderLink(s) {
    const link = s.link || {};
    const box = $("link");
    const test = link.test || {};
    const connected = link.source === "tickerall";
    let cls, title, detail;
    if (connected && link.mode === "tickerall") {
      cls = "ok"; title = "الوسيط متصل · التنفيذ على الحساب التجريبي";
      detail = "الرصيد والأرباح تُقرأ مباشرة من الوسيط.";
    } else if (connected) {
      cls = "warn"; title = "الوسيط متصل · التنفيذ ورقي حالياً";
      detail = "الصفقات محاكاة فقط. لتنفيذها على الحساب التجريبي اضبط DEFAULT_MODE=tickerall في Render.";
    } else if (!link.has_credentials) {
      cls = "bad"; title = "الوسيط غير متصل: بيانات الحساب غير موجودة";
      detail = "أضف في Render المتغيرات: TICKERALL_API_KEY و MT5_SERVER و MT5_ACCOUNT_ID و MT5_PASSWORD ثم أعد النشر.";
    } else if (test.ok === false) {
      cls = "bad"; title = "الوسيط غير متصل";
      detail = "سبب الفشل: " + (test.error || "غير معروف") + ". راجع بيانات الحساب في Render.";
    } else {
      cls = "warn"; title = "الوسيط غير متصل بعد";
      detail = "جارٍ المحاولة. إذا استمرت الحالة راجع سجلات Render.";
    }
    box.className = "link " + cls;
    box.replaceChildren();
    const b = document.createElement("b");
    b.textContent = title;
    const d = document.createElement("span");
    d.textContent = detail;
    box.append(b, d);
    $("liveDot").className = "dot " + (cls === "ok" ? "ok" : cls === "bad" ? "bad" : "warn");
  }

  function renderAccount(s) {
    const acc = s.account || {};
    const connected = acc.source === "tickerall" && acc.balance != null;
    if (connected) {
      $("balance").textContent = fmt(acc.balance) + " $";
      $("balanceSub").textContent = acc.equity == null ? "حساب تجريبي"
        : "الأصول الحالية " + fmt(acc.equity) + " $";
    } else {
      $("balance").textContent = "—";
      $("balanceSub").textContent = "يظهر بعد اتصال الوسيط";
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
      const card = document.createElement("div");
      card.className = "bot";

      const name = document.createElement("div");
      name.className = "name";
      name.textContent = b.name;
      const runningOn = b.running.map((a) => ASSET_NAME[a]).filter(Boolean);
      if (runningOn.length) {
        const tag = document.createElement("span");
        tag.className = "running";
        tag.textContent = "يعمل · " + runningOn.join("، ");
        name.append(tag);
      }
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${b.trades} صفقة مغلقة · ربح ${signed(b.profit)}$`;

      const btns = document.createElement("div");
      btns.className = "btns";
      for (const a of assets) {
        const running = b.running.includes(a);
        const blockedWhy = (board.blocked && board.blocked[a]) || (b.blocked && b.blocked[a]);
        const btn = document.createElement("button");
        btn.dataset.asset = a;
        btn.dataset.bot = b.id;
        btn.dataset.stop = running ? "1" : "0";
        if (running) {
          btn.textContent = "إيقاف · " + ASSET_NAME[a];
          btn.className = "stop";
        } else if (blockedWhy) {
          btn.textContent = ASSET_NAME[a] + " محظور";
          btn.disabled = true;
          btn.title = blockedWhy;
        } else {
          btn.textContent = "تشغيل · " + ASSET_NAME[a];
          btn.className = "on";
        }
        btns.append(btn);
      }
      card.append(name, meta, btns);
      list.append(card);
    }
  }

  async function refresh() {
    try {
      const s = await getState();
      renderLink(s);
      renderAccount(s);
      renderTotals(s.board || { bots: [] });
      renderBots(s);
      $("liveText").textContent = "آخر تحديث " + new Date().toLocaleTimeString("ar-SA");
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

  async function exportCandles(asset) {
    const pwd = $("pw").value.trim();
    if (!pwd) { msg("أدخل كلمة مرور الإدارة أولاً", "err"); $("pw").focus(); return; }
    msg("جارٍ تجهيز الشموع…", "");
    try {
      const r = await fetch("/api/candles.csv?asset=" + encodeURIComponent(asset), {
        headers: { "X-Admin-Password": pwd },
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.error || "فشل التصدير (" + r.status + ")");
      }
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = asset + "_H1.csv";
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      msg("تم تنزيل ملف الشموع", "ok");
    } catch (err) {
      msg(err.message, "err");
    }
  }
  $("expXAU").addEventListener("click", () => exportCandles("XAUUSD"));
  $("expBTC").addEventListener("click", () => exportCandles("BTCUSDT"));

  async function histStart(symbols, intervals) {
    const pwd = $("pw").value.trim();
    if (!pwd) { msg("أدخل كلمة مرور الإدارة أولاً", "err"); $("pw").focus(); return; }
    try {
      const r = await fetch("/api/history/start", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Admin-Password": pwd },
        body: JSON.stringify({ symbols, intervals, days: 365 }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || "فشل الطلب (" + r.status + ")");
      msg(data.note || "بدأ السحب", "ok");
      histRefresh();
    } catch (err) { msg(err.message, "err"); }
  }

  async function histRefresh() {
    const pwd = $("pw").value.trim();
    if (!pwd) return;
    try {
      const r = await fetch("/api/history/status", { headers: { "X-Admin-Password": pwd }, cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      const jobs = Object.entries(s.jobs || {});
      const done = jobs.filter(([, j]) => j.status === "done").length;
      const failed = jobs.filter(([, j]) => j.status === "error");
      const lines = [
        s.running ? "السحب قيد التنفيذ…" : (s.finished ? "انتهى السحب في " + s.finished : "لم يبدأ أي سحب بعد"),
        "مكتمل: " + done + " من " + jobs.length + (failed.length ? " · أخطاء: " + failed.length : ""),
      ];
      for (const [k, j] of failed.slice(0, 5)) lines.push("✗ " + k + ": " + (j.error || ""));
      $("histOut").textContent = lines.join("\n");
    } catch (e) {}
  }

  async function histZip() {
    const pwd = $("pw").value.trim();
    if (!pwd) { msg("أدخل كلمة مرور الإدارة أولاً", "err"); $("pw").focus(); return; }
    try {
      const r = await fetch("/api/history/zip", { headers: { "X-Admin-Password": pwd } });
      if (!r.ok) throw new Error("فشل التحميل (" + r.status + ")");
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url; a.download = "history.zip";
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { msg(err.message, "err"); }
  }

  $("histMain").addEventListener("click", () => histStart("assets", ["1h", "1d", "1w", "1mo"]));
  $("histAll").addEventListener("click", () => histStart("all", ["1h", "1d", "1w", "1mo"]));
  $("histZip").addEventListener("click", histZip);
  setInterval(histRefresh, 10000);

  refresh();
  setInterval(refresh, 5000);
})();
