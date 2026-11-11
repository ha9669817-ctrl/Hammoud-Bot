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

// هياكل تحميل قبل وصول أول بيانات
document.getElementById("priceCards").innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';

function renderStatusBar(s) {
  const t = s.trading || {};
  const e = s.engine || {};
  const tr = document.getElementById("sbTrade");
  tr.className = "pill " + (t.master ? "up" : "down");
  tr.textContent = t.master ? "التداول: يعمل" : "التداول: متوقف";
  const en = document.getElementById("sbEngine");
  const evolving = !!e.evolving;
  en.className = "pill " + (evolving ? "gold" : e.status === "analyzing" ? "info" : "");
  en.textContent = evolving ? "المحرك: يطوّر" : e.status === "analyzing" ? "المحرك: يفحص" : "المحرك: جاهز";
  document.getElementById("sbMode").textContent = "الوضع: " + (MODE_AR[e.mode] || e.mode || "—");
  document.getElementById("sbLast").textContent = e.last_run ? "آخر فحص " + hm(e.last_run * 1000) : "";
  document.getElementById("verFoot").textContent = "المالك والمطور: HAMAD · الإصدار: " + (s.version || "محلي");
}

let stateFails = 0;
async function fetchJson(url, ms = 6000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    const r = await fetch(url, { cache: "no-store", signal: ctrl.signal });
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

async function loadState() {
  try {
    state = await fetchJson("/api/state");
    stateFails = 0;
    renderState(state);
    $("liveDot").className = "dot live";
    const busy = state.engine && state.engine.status === "analyzing";
    $("liveText").textContent = busy
      ? "المحرك يفحص الأسواق الآن..."
      : "متصل · تحديث الحالة " + new Date().toLocaleTimeString("ar-SA");
  } catch (e) {
    stateFails += 1;
    // لا نعرض خطأ أحمر إلا بعد 3 محاولات فاشلة متتالية، ونبقي آخر حالة معروضة
    if (stateFails >= 3) {
      $("liveDot").className = "dot down";
      $("liveText").textContent = "انقطع الاتصال بالخادم، يُعاد المحاولة...";
    } else {
      $("liveText").textContent = "جارٍ التحديث...";
    }
  }
}

async function loadMarket(force = false) {
  try {
    market = await fetchJson("/api/market", 5000);
    renderPrices();
  } catch (e) {
    // نبقي آخر أسعار معروضة بدل إظهار خطأ
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
    const open = a.session_open !== undefined ? a.session_open : (sess[k] ? sess[k].session_open : null);
    const proxyNote = a.proxy
      ? `<div class="muted" style="font-size:12px;margin-top:6px">${open === false
          ? "السوق الرسمي مغلق الآن. الرقم المعروض من PAXG الذي يتداول 24/7، وليس سعر الذهب الرسمي"
          : "السعر من PAXG كمرجع تقريبي، وليس سعر الذهب الرسمي"}</div>`
      : "";
    const pill = open === null ? "" : `<span class="pill ${open ? "up" : "down"}"><span class="dot ${open ? "up" : "down"}"></span>${open ? "السوق مفتوح" : "السوق مغلق"}</span>`;
    const body = a.error
      ? `<div class="down" style="margin-top:10px;font-size:13px">${esc(a.error)}</div>`
      : `<div class="pval">${num(a.price)}</div>
         <div class="pchg ${sgn(a.change_pct)}" style="margin-top:4px">${a.change_pct > 0 ? "+" : ""}${num(a.change_pct, 3)}% <span class="muted">· آخر 3 ساعات</span></div>`;
    const opensTxt = (!open && sess[k] && sess[k].next_open ? `<div class="muted" style="font-size:12px;margin-top:6px">يفتح ${fmtOpen(sess[k].next_open)} (بتوقيت الرياض)</div>` : "") + proxyNote;
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
  renderTrading(s.trading, s.assets);
  renderBots(s.bots || [], s.assets);
  renderStatusBar(s);
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
    : `<tr><td colspan="6" class="empty">لا توجد صفقات مفتوحة. تُفتح الصفقات عند تشغيل التداول وظهور إشارة من الاستراتيجية المعتمدة.</td></tr>`;
  const unmanaged = Object.values(s.assets).flatMap(v => ((v.live && v.live.ta_positions) || []).filter(p => !p.managed));
  $("unmanagedBox").classList.toggle("hidden", unmanaged.length === 0);
  $("unmanagedRows").innerHTML = unmanaged.map(p => `<tr><td>${esc(p.symbol)}</td><td>${sideTxt(p.side)}</td>
    <td class="mono">${num(p.volume)}</td><td class="mono ${sgn(p.profit)}">${num(p.profit)}</td>
    <td class="mono">${num(p.stopLoss)}</td><td class="mono">${num(p.takeProfit)}</td></tr>`).join("");
}

function renderClosed(closed) {
  if (closed) renderClosed.last = closed;
  const all = renderClosed.last || [];
  const f = $("closedFilter").value, a = $("closedAsset").value;
  const list = all.filter(t => (f === "all" || (f === "win" ? t.r > 0 : t.r <= 0)) && (a === "all" || t.asset === a));
  $("closedRows").innerHTML = list.length ? list.map(t => `<tr>
      <td>${esc(t.asset)}</td><td>${sideTxt(t.side)}</td>
      <td class="mono ${sgn(t.r)}">${num(t.r)}R</td>
      <td class="mono ${sgn(t.pnl_usd)}">${t.pnl_usd > 0 ? "+" : ""}${num(t.pnl_usd)}$</td>
      <td class="muted mono">${dt(t.closed_utc)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">${all.length ? "لا توجد صفقات تطابق الفلتر" : "لا توجد صفقات مغلقة بعد. ستظهر هنا بعد أول صفقة تُغلق."}</td></tr>`;
}
$("closedFilter").onchange = () => renderClosed();
$("closedAsset").onchange = () => renderClosed();

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
let _botsKey = "";
function renderBots(bots, assets) {
  const key = JSON.stringify(bots);
  if (key === _botsKey) return;
  _botsKey = key;
  const el = $("botList");
  el.innerHTML = bots.map(b => `
    <div class="bot-row" style="display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:10px;border:1px solid var(--line,#2a2f3a);border-radius:10px">
      <span style="font-weight:600">${b.name}</span>
      <span style="display:flex;gap:6px">
        <button class="btn btn-primary" data-bot="${b.id}" data-asset="BTCUSDT">تشغيل · BTC</button>
        <button class="btn" data-bot="${b.id}" data-asset="XAUUSD">تشغيل · الذهب</button>
        <button class="btn" data-stop="BTCUSDT">إيقاف BTC</button>
        <button class="btn" data-stop="XAUUSD">إيقاف الذهب</button>
      </span>
    </div>`).join("");
  el.onclick = async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    const res = b.dataset.stop
      ? await postJson("/api/bot/stop", { asset: b.dataset.stop })
      : await postJson("/api/bot/start", { asset: b.dataset.asset, bot: b.dataset.bot });
    report(res);
  };
}

function renderTrading(t, assets) {
  if (!t) return;
  $("btnMaster").textContent = t.master ? "إيقاف التداول" : "تشغيل التداول";
  $("tradingState").textContent = t.master ? "التداول مفعّل" : "التداول متوقف (يبدأ متوقفاً بعد كل نشر)";
  const html = Object.entries(assets).map(([k, a]) => {
    const on = !!(t.assets && t.assets[k]);
    const locked = !!(t.locked && t.locked[k]);
    const board = a.leaderboard || [];
    const opts = board.length
      ? board.map((x, i) => `<option value="${i}">${esc(describe(x.genome))} · PF ${num(x.result.val.profit_factor)}</option>`).join("")
      : `<option value="">لا توجد استراتيجيات معتمدة</option>`;
    return `<div class="panel" style="padding:14px;margin-top:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="title">${esc(a.label)}</span>
        <button class="btn" data-toggle="${k}" data-on="${on ? 0 : 1}">${on ? "إيقاف هذا الأصل" : "تشغيل هذا الأصل"}</button>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">${locked ? "استراتيجية مختارة يدوياً، لن يغيّرها التطوير" : "الاستراتيجية تُختار تلقائياً من التطوير"}</div>
      <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
        <select data-select="${k}" style="flex:1;min-width:200px">${opts}</select>
        <button class="btn" data-choose="${k}" ${board.length ? "" : "disabled"}>اعتماد</button>
        ${locked ? `<button class="btn" data-unlock="${k}">إلغاء الاختيار اليدوي</button>` : ""}
      </div></div>`;
  }).join("");
  // نعيد البناء فقط عند تغيّر المحتوى، حتى لا يُلغى اختيار القائمة أثناء التحديث الدوري
  if (html !== renderTrading.last) { renderTrading.last = html; $("assetCtl").innerHTML = html; }
}

$("btnMaster").onclick = async () => {
  const on = !(state && state.trading && state.trading.master);
  report(await postJson("/api/trading/master", { on }));
};
$("assetCtl").onclick = async (e) => {
  const el = e.target.closest("button");
  if (!el) return;
  if (el.dataset.toggle) return report(await postJson("/api/trading/asset", { asset: el.dataset.toggle, on: el.dataset.on === "1" }));
  if (el.dataset.choose) {
    const idx = Number(document.querySelector(`select[data-select="${el.dataset.choose}"]`).value);
    return report(await postJson("/api/strategy/select", { asset: el.dataset.choose, index: idx }));
  }
  if (el.dataset.unlock) return report(await postJson("/api/strategy/unlock", { asset: el.dataset.unlock }));
};

function renderBoard(assets) {
  const board = Object.entries(assets).flatMap(([k, v]) => v.leaderboard.map(x => ({...x, asset: v.label})));
  $("boardRows").innerHTML = board.length ? board.map(x => `<tr>
      <td>${esc(x.asset)}</td><td class="mono muted" style="font-size:12px">${esc(describe(x.genome))}</td>
      <td class="mono">${num(x.result.val.win_rate, 1)}</td><td class="mono">${num(x.result.val.profit_factor)}</td>
      <td class="mono">${num(x.result.val.max_dd_r)}</td><td class="mono gold">${num(x.score, 3)}</td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">لم تُعتمد أي استراتيجية بعد. شرط الاعتماد اجتياز اختبار التحقق بعدد صفقات كافٍ وعامل ربح مقبول. اضغط "تطوير" للبحث من جديد.</td></tr>`;
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
