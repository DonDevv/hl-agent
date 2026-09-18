/* hl-agent dashboard: the whole project on one page (home, strategies, runs, traders, more). */
(() => {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Logo de l'actif (icônes publiques de l'app Hyperliquid) ; le ticker reste derrière si l'image manque.
  const coinIcon = (asset, size = 40) => {
    const sym = String(asset || "").split(":").pop().replace(/^k/, "");
    return `<div class="icon coin" style="width:${size}px;height:${size}px"><span>${esc(sym.slice(0, 4))}</span><img src="/coins/${encodeURIComponent(sym)}.svg" alt="" loading="lazy" onerror="this.remove()"></div>`;
  };
  const coinInline = (asset) => `<span class="coin-inline">${coinIcon(asset, 18)}${esc(asset)}</span>`;

  const state = {
    view: "home",
    range: "all",
    data: null, // /api/state
    equity: [],
    equityRun: null,
    strategies: null,
    stratFilter: "all",
    stratQuery: "",
    runFilter: "all",
    runQuery: "",
    traders: null,
    jobs: [],
    cache: null,
    settings: null,
    sheet: null, // { type, id, poll }
    card: null, // trade affiché dans la carte de partage
    trades: [], // trades du run ouvert (pour la carte)
    subtab: {},
  };

  // ---- formatting ----------------------------------------------------------------

  const fmtUsd = (v, d) => {
    if (v == null || Number.isNaN(v)) return "—";
    const n = Number(v);
    const digits = d ?? (Math.abs(n) >= 1000 ? 0 : 2);
    // Prix < 1 $ (PUMP, ARB…) : garder 4 chiffres significatifs plutôt que d'afficher $0.00.
    const sig = d == null && Math.abs(n) > 0 && Math.abs(n) < 1 ? Math.max(digits, 3 - Math.floor(Math.log10(Math.abs(n)))) : digits;
    return (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: sig });
  };
  const fmtPct = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(d)}%`);
  const fmtNum = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d }));
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const ago = (ms) => {
    if (!ms) return "—";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return `il y a ${Math.round(s)}s`;
    if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
    if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
    return `il y a ${Math.round(s / 86400)} j`;
  };
  const date = (ms) => (ms ? new Date(ms).toISOString().slice(0, 10) : "—");
  const when = (ms) => (ms ? new Date(ms).toLocaleString("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
  const win = (w) => (w && w.length === 2 ? `${date(w[0])} → ${date(w[1])}` : "");
  const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");
  const dur = (a, b) => {
    if (!a || !b) return "";
    const s = Math.round((b - a) / 1000);
    return s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
  };
  const ICONS = { fetch: "⬇️", validate: "🔍", backtest: "📈", walkforward: "🧪", run: "🚀" };
  const TITLES = { validate: "Valider", backtest: "Backtest", walkforward: "Walk-forward", run: "Passer en live" };

  // ---- api -------------------------------------------------------------------------

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "content-type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin",
    });
    if (res.status === 401) {
      if (state.view !== "more") setView("more");
      toast("Connecte-toi avec ton token web");
      throw new Error("unauthorized");
    }
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || res.statusText);
    return j;
  }

  let toastTimer = null;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 3000);
  }

  // ---- navigation ------------------------------------------------------------------

  const LOADERS = {
    home: refreshState,
    strategies: loadStrategies,
    runs: refreshState,
    traders: loadTraders,
    more: loadMore,
  };

  function setView(v) {
    state.view = v;
    closeSheet();
    $$(".view").forEach((el) => el.classList.toggle("hidden", el.id !== `view-${v}`));
    $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    window.scrollTo(0, 0);
    LOADERS[v]().catch((e) => toast(e.message));
  }

  $$(".tabs button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

  // ---- carte de trade (partage) -------------------------------------------------------
  // Une carte façon "PnL card" : position ouverte (rafraîchie à chaque poll) ou trade fermé.
  const imgCache = {};
  const loadImg = (src) => imgCache[src] || (imgCache[src] = new Promise((res) => {
    const im = new Image(); im.onload = () => res(im); im.onerror = () => res(null); im.src = src;
  }));
  const rr = (ctx, x, y, w, h, r) => { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); };
  // Même police que la carte Hyperliquid (Inter Bold) pour tous les chiffres.
  const FONT = "Inter, -apple-system, 'Segoe UI', sans-serif";
  // Le gros pourcentage HL est en Teodor Light (police commerciale) : on la sert depuis
  // /static/fonts/Teodor-Light.woff2 si le fichier est là, sinon Instrument Serif (sosie libre).
  const NUMFONT = "Teodor, 'Instrument Serif', Georgia, serif";
  const fontsReady = document.fonts ? Promise.all(["500 24px Inter", "600 34px Inter", "700 40px Inter", "300 160px Teodor", "400 160px 'Instrument Serif'"].map((f) => document.fonts.load(f).catch(() => null))) : Promise.resolve();
  // Prix façon HL : virgule décimale, pas de symbole, 5 chiffres significatifs sous 1.
  const fmtPx = (v) => {
    if (v == null || !isFinite(Number(v))) return "—";
    const n = Math.abs(Number(v));
    const dec = n >= 1 ? 2 : Math.min(8, 4 - Math.floor(Math.log10(n || 1)));
    return Number(v).toLocaleString("fr-FR", { minimumFractionDigits: Math.min(dec, 2), maximumFractionDigits: dec });
  };
  // Flèche "maison" de la carte HL (coins arrondis), centrée en (0,0), pointe vers le haut.
  const arrowPath = (ctx) => {
    ctx.beginPath();
    ctx.moveTo(0, -88); ctx.lineTo(84, 0); ctx.lineTo(38, 0); ctx.lineTo(38, 62); ctx.lineTo(-38, 62); ctx.lineTo(-38, 0); ctx.lineTo(-84, 0); ctx.closePath();
  };
  // Réplique de la carte de partage Hyperliquid (975×697, rendue en 2x).
  async function drawCard(c, t) {
    await fontsReady;
    const W = 975, H = 697, S = 2;
    c.width = W * S; c.height = H * S;
    const ctx = c.getContext("2d"); ctx.scale(S, S);
    const up = (t.roe ?? 0) >= 0;
    const accent = up ? "#50d2c1" : "#ed7088", rgb = up ? "80,210,193" : "237,112,136";
    // fond + coins arrondis + liseré
    rr(ctx, 0, 0, W, H, 28); ctx.save(); ctx.clip();
    const g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, "#0e1a1e"); g.addColorStop(1, "#070f13");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    // anneaux : contours décalés de la flèche (trait épais − trait plus fin), du plus loin au plus près
    const o = document.createElement("canvas"); o.width = W * S; o.height = H * S;
    const oc = o.getContext("2d"); oc.scale(S, S); oc.translate(688, 356); if (!up) oc.scale(1, -1); oc.lineJoin = "round"; oc.lineCap = "round";
    const rings = [];
    for (let d = 14, k = 0; d < 720; k++, d += 12 + k * 0.45) rings.push(d);
    for (const d of rings.reverse()) {
      const alpha = Math.max(0.05, 0.85 * Math.exp(-d / 150));
      arrowPath(oc); oc.globalCompositeOperation = "source-over"; oc.strokeStyle = `rgba(${rgb},${alpha.toFixed(3)})`; oc.lineWidth = d * 2 + 1.4; oc.stroke();
      arrowPath(oc); oc.globalCompositeOperation = "destination-out"; oc.strokeStyle = "#000"; oc.lineWidth = d * 2 - 1.4; oc.stroke();
    }
    // intérieur de la flèche : nettoyé, teinte légère, un seul contour intérieur (comme HL)
    arrowPath(oc); oc.globalCompositeOperation = "destination-out"; oc.fill();
    oc.globalCompositeOperation = "source-over";
    arrowPath(oc); oc.fillStyle = `rgba(${rgb},0.10)`; oc.fill();
    oc.save(); arrowPath(oc); oc.clip();
    arrowPath(oc); oc.strokeStyle = `rgba(${rgb},0.7)`; oc.lineWidth = 2 * 11 + 1.4; oc.stroke();
    arrowPath(oc); oc.globalCompositeOperation = "destination-out"; oc.lineWidth = 2 * 11 - 1.4; oc.stroke();
    oc.globalCompositeOperation = "source-over"; arrowPath(oc); oc.fillStyle = `rgba(${rgb},0.10)`; oc.fill();
    oc.restore();
    arrowPath(oc); oc.strokeStyle = accent; oc.lineWidth = 2.4; oc.stroke();
    ctx.drawImage(o, 0, 0, W, H);
    ctx.restore();
    rr(ctx, 0.5, 0.5, W - 1, H - 1, 28); ctx.strokeStyle = "rgba(255,255,255,0.09)"; ctx.lineWidth = 1; ctx.stroke();
    // marque
    const logo = await loadImg("/static/logo.png");
    ctx.textBaseline = "middle"; ctx.textAlign = "left";
    if (logo) ctx.drawImage(logo, 50, 60, 40, 40);
    ctx.fillStyle = "#f6fefd"; ctx.font = `500 30px 'Noto Sans JP', ${FONT}`; ctx.fillText("俺び寂び", 104, 80);
    // actif + sens
    const sym = String(t.asset).split(":").pop().replace(/^k/, "");
    const coin = await loadImg(`/coins/${encodeURIComponent(sym)}.svg`);
    let x = 52, y = 256;
    if (coin) { ctx.save(); ctx.beginPath(); ctx.arc(x + 18, y, 18, 0, Math.PI * 2); ctx.clip(); ctx.fillStyle = "#fff"; ctx.fillRect(x, y - 18, 36, 36); ctx.drawImage(coin, x, y - 18, 36, 36); ctx.restore(); x += 50; }
    ctx.fillStyle = "#f6fefd"; ctx.font = `500 24px ${FONT}`; ctx.fillText(t.asset, x, y);
    x += ctx.measureText(t.asset).width + 16;
    const label = `${t.direction === "LONG" ? "LONG" : "SHORT"} ${t.leverage ? Math.round(t.leverage) + "X" : ""}`.trim();
    ctx.font = `500 22px ${FONT}`;
    const lw = ctx.measureText(label).width + 24;
    // la pastille reste teal sur HL, même sur un trade perdant
    rr(ctx, x, y - 18, lw, 36, 6); ctx.fillStyle = "#173f3c"; ctx.fill();
    ctx.fillStyle = "#50d2c1"; ctx.fillText(label, x + 12, y + 1);
    // ROE (Teodor Light)
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = accent; ctx.font = `300 122px ${NUMFONT}`;
    ctx.fillText(t.roe == null ? "—" : `${t.roe >= 0 ? "+" : "-"}${Math.abs(t.roe).toFixed(1).replace(".", ",")}%`, 48, 412);
    // prix
    const cols = [["Prix d'entrée", fmtPx(t.entry)], [t.closed ? "Prix de sortie" : "Prix actuel", fmtPx(t.price)]];
    x = 52;
    for (const [k, v] of cols) {
      ctx.fillStyle = "#949e9c"; ctx.font = `400 22px ${FONT}`; ctx.fillText(k, x, 526);
      const kw = ctx.measureText(k).width;
      ctx.fillStyle = "#f6fefd"; ctx.font = `400 24px ${FONT}`; ctx.fillText(v, x, 564);
      x += Math.max(kw, ctx.measureText(v).width) + 40;
    }
    // pied : réseau + état
    ctx.fillStyle = "#949e9c"; ctx.font = `400 22px ${FONT}`;
    ctx.fillText(`hl-agent · ${t.network || ""}${t.pnl != null ? ` · PnL ${t.pnl >= 0 ? "+" : "−"}${fmtUsd(Math.abs(t.pnl), 2)}` : ""}`, 52, 613);
    ctx.fillStyle = "#f6fefd"; ctx.font = `400 24px ${FONT}`;
    ctx.fillText(t.closed ? `Fermé · ${when(t.closed_ms)}${t.reason ? " · " + t.reason : ""}` : `En cours · ${when(Date.now())}`, 52, 651);
  }
  function openCard(t) {
    state.card = t;
    $("#card").classList.remove("hidden");
    $("#card .card-live").classList.toggle("hidden", !!t.closed);
    drawCard($("#card-canvas"), t).catch(() => {});
  }
  function closeCard() { state.card = null; $("#card").classList.add("hidden"); }
  // Position ouverte → la carte suit le poll du compte.
  function refreshCard() {
    const t = state.card;
    if (!t || t.closed || !state.data) return;
    const p = state.data.account.positions.find((q) => q.asset === t.asset);
    if (!p) { t.closed = true; t.closed_ms = Date.now(); $("#card .card-live").classList.add("hidden"); }
    else Object.assign(t, { roe: p.roe, pnl: p.upnl, price: p.price, entry: p.entry, leverage: p.leverage, direction: p.direction });
    drawCard($("#card-canvas"), t).catch(() => {});
  }
  const cardBlob = () => new Promise((res) => $("#card-canvas").toBlob(res, "image/png"));
  const cardName = () => `${String(state.card?.asset || "trade").replace(/[^A-Za-z0-9]/g, "")}-${state.card?.direction || ""}-${new Date().toISOString().slice(0, 10)}.png`;
  $("#card-close").addEventListener("click", closeCard);
  $(".card-backdrop").addEventListener("click", closeCard);
  $("#card-save").addEventListener("click", async () => {
    const b = await cardBlob(); if (!b) return;
    const a = document.createElement("a"); a.href = URL.createObjectURL(b); a.download = cardName(); a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  });
  $("#card-share").addEventListener("click", async () => {
    const b = await cardBlob(); if (!b) return;
    const file = new File([b], cardName(), { type: "image/png" });
    const t = state.card, txt = `${t.asset} ${t.direction} ${Math.round(t.leverage || 0)}x · ${fmtPct(t.roe)} — 俺び寂び`;
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try { await navigator.share({ files: [file], text: txt }); } catch (e) {}
    } else $("#card-save").click();
  });
  $("#positions").addEventListener("click", (e) => {
    const el = e.target.closest("[data-card-pos]"); if (!el) return;
    const p = state.data.account.positions[Number(el.dataset.cardPos)]; if (!p) return;
    openCard({ asset: p.asset, direction: p.direction, leverage: p.leverage, roe: p.roe, pnl: p.upnl, entry: p.entry, price: p.price, network: state.data.network, closed: false });
  });

  // ---- sheet -----------------------------------------------------------------------

  function openSheet(type, id, html) {
    if (state.sheet && state.sheet.poll) clearInterval(state.sheet.poll);
    state.sheet = { type, id, poll: null };
    $("#sheet-body").innerHTML = html;
    $("#sheet").classList.remove("hidden");
    $(".sheet-panel").scrollTop = 0;
    document.body.style.overflow = "hidden";
  }
  function closeSheet() {
    if (state.sheet && state.sheet.poll) clearInterval(state.sheet.poll);
    state.sheet = null;
    $("#sheet").classList.add("hidden");
    document.body.style.overflow = "";
  }
  $(".sheet-backdrop").addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeCard(); closeSheet(); } });

  function subtabs(key, tabs) {
    const cur = tabs.some((t) => t[0] === state.subtab[key]) ? state.subtab[key] : tabs[0][0];
    return `<div class="subtabs" data-key="${esc(key)}">${tabs
      .map(([id, label]) => `<button type="button" data-tab="${esc(id)}" class="${id === cur ? "on" : ""}">${esc(label)}</button>`)
      .join("")}</div>${tabs.map(([id, , html]) => `<div data-pane="${esc(id)}" class="${id === cur ? "" : "hidden"}">${html}</div>`).join("")}`;
  }
  $("#sheet-body").addEventListener("click", (e) => {
    const tr = e.target.closest("[data-card-trade]");
    if (tr) {
      const t = (state.trades || [])[Number(tr.dataset.cardTrade)]; if (!t) return;
      return openCard({ asset: t.asset, direction: t.direction, leverage: t.leverage, roe: t.roe_pct, pnl: t.pnl_usd, entry: t.entry_price, price: t.exit_price, closed: true, closed_ms: t.closed_ms, reason: t.reason, network: state.data?.network });
    }
    const b = e.target.closest(".subtabs button");
    if (!b) return;
    const key = b.parentElement.dataset.key;
    state.subtab[key] = b.dataset.tab;
    $$(".subtabs button", b.parentElement).forEach((x) => x.classList.toggle("on", x === b));
    $$("#sheet-body [data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== b.dataset.tab));
  });

  // ---- chart -----------------------------------------------------------------------

  function drawChart(svg, points, range) {
    const W = 360, H = svg.viewBox.baseVal.height || 140, PAD = 6;
    let pts = points;
    if (range !== "all" && pts.length) {
      const span = range === "1d" ? 86400e3 : 7 * 86400e3;
      const since = pts[pts.length - 1][0] - span;
      const cut = pts.filter((p) => p[0] >= since);
      pts = cut.length > 1 ? cut : pts.slice(-2);
    }
    if (pts.length < 2) {
      svg.innerHTML = "";
      return null;
    }
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const x0 = xs[0], x1 = xs[xs.length - 1];
    let lo = Infinity, hi = -Infinity;
    for (const y of ys) { if (y < lo) lo = y; if (y > hi) hi = y; }
    const sx = (x) => (x1 === x0 ? 0 : ((x - x0) / (x1 - x0)) * W);
    const sy = (y) => (hi === lo ? H / 2 : PAD + (1 - (y - lo) / (hi - lo)) * (H - 2 * PAD));
    const d = pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    const dir = ys[ys.length - 1] >= ys[0] ? "up" : "down";
    svg.innerHTML = `<path class="area ${dir}" d="${d}L${W},${H}L0,${H}Z"/><path class="line ${dir}" d="${d}"/>`;
    return { first: pts[0], last: pts[pts.length - 1], lo, hi };
  }

  // ---- home ------------------------------------------------------------------------

  function runIcon(r) {
    if (r.kind === "live") return `<div class="icon" style="color:#128a3e">LIVE</div>`;
    if (r.kind === "walkforward") return `<div class="icon">WF</div>`;
    return `<div class="icon">BT</div>`;
  }
  function runItem(r) {
    const ret = r.return_pct;
    const sub = r.kind === "live"
      ? `${r.copy ? "copie " + short(r.copy) : r.strategy || r.package || ""} · ${r.alive ? "tick " + ago(r.last_tick_ms) : "arrêté " + ago(r.last_tick_ms)}`
      : r.kind === "walkforward"
        ? `${r.strategy || ""} · ${r.folds.length} folds · ${r.profitable_folds}/${r.folds.length} gagnants`
        : `${r.strategy || ""} · ${r.window ? win(r.window) : date(r.first_ms)}`;
    const badge = r.kind === "live"
      ? (r.stop ? `<span class="pill pill-red">STOP</span>` : r.alive ? `<span class="pill pill-green">en cours</span>` : `<span class="pill pill-muted">inactif</span>`)
      : "";
    return `<button class="item" data-run="${esc(r.name)}">
      ${runIcon(r)}
      <div class="main"><div class="title"><span class="name">${esc(r.name)}</span>${badge}</div><div class="sub">${esc(sub)}</div></div>
      <div class="right"><div class="v ${cls(ret)}">${fmtPct(ret)}</div><div class="s">${r.trades ?? 0} trades · DD ${r.max_dd_pct == null ? "—" : r.max_dd_pct.toFixed(1) + "%"}</div></div>
    </button>`;
  }

  async function refreshState() {
    state.data = await api("/api/state");
    renderHeader();
    refreshCard();
    if (state.view === "home") await renderHome();
    if (state.view === "runs") renderRuns();
  }

  function renderHeader() {
    const s = state.data;
    if (!s) return;
    const net = $("#net");
    net.textContent = s.network;
    net.className = `pill ${s.network === "mainnet" ? "pill-red" : "pill-blue"}`;
    const key = s.agent_key || "";
    const ok = /authori[sz]ed|valid|ok/i.test(key) && !/not |missing|no key|unset|invalid/i.test(key);
    const dot = $("#key-dot");
    dot.className = `dot ${key ? (ok ? "dot-green" : "dot-amber") : "dot-muted"}`;
    dot.title = key || "pas de clé agent";
    const running = state.jobs.filter((j) => j.running).length;
    const jd = $("#jobs-dot");
    jd.textContent = `${running} job${running > 1 ? "s" : ""}`;
    jd.classList.toggle("hidden", running === 0);
  }

  async function renderHome() {
    const s = state.data;
    const acct = s.account;
    $("#balance").textContent = fmtUsd(acct.value, 2);
    const upnl = acct.positions.reduce((a, p) => a + (p.upnl || 0), 0);
    const delta = $("#delta");
    delta.className = `delta ${cls(upnl)}`;
    delta.textContent = `${fmtUsd(upnl, 2)} latent · ${fmtUsd(acct.withdrawable, 2)} dispo`;

    const pos = $("#positions");
    pos.innerHTML = acct.positions.length
      ? acct.positions.map((p, i) => `<div class="item tap" data-card-pos="${i}">
          ${coinIcon(p.asset)}
          <div class="main"><div class="title"><span class="name">${esc(p.asset)}</span><span class="pill ${p.direction === "LONG" ? "pill-green" : "pill-red"}">${p.direction === "LONG" ? "Long" : "Short"} ${p.leverage}x</span></div>
          <div class="sub">${fmtNum(p.size, 4)} @ ${fmtUsd(p.entry, 2)} · cours ${fmtUsd(p.price, 2)} · liq ${fmtUsd(p.liquidation, 0)}</div></div>
          <div class="right"><div class="v ${cls(p.upnl)}">${fmtUsd(p.upnl, 2)}</div><div class="s ${cls(p.roe)}">${fmtPct(p.roe)}</div></div>
        </div>`).join("")
      : `<div class="empty">Aucune position ouverte</div>`;

    const live = s.runs.filter((r) => r.kind === "live");
    $("#live-runs").innerHTML = live.length ? live.map(runItem).join("") : `<div class="empty">Aucun agent live — lance-en un depuis une stratégie</div>`;
    const recent = s.runs.filter((r) => r.kind !== "live").slice(0, 5);
    $("#recent-runs").innerHTML = recent.length ? recent.map(runItem).join("") : `<div class="empty">Aucun backtest pour l’instant</div>`;

    const target = live[0] || s.runs[0];
    $("#chart-title").textContent = target ? `${target.kind === "live" ? "Valeur du compte" : "Équité"} · ${target.name}` : "Valeur du compte";
    if (target) {
      const name = target.kind === "walkforward" ? target.folds[target.folds.length - 1].name : target.name;
      if (state.equityRun !== name || target.kind === "live") {
        state.equity = await api(`/api/runs/${name}/equity`);
        state.equityRun = name;
      }
    } else {
      state.equity = [];
    }
    renderChart();
  }

  function renderChart() {
    const r = drawChart($("#chart"), state.equity, state.range);
    const foot = $("#chart-foot");
    if (!r) {
      foot.innerHTML = `<span>pas encore d’équité</span>`;
      return;
    }
    const chg = ((r.last[1] - r.first[1]) / r.first[1]) * 100;
    foot.innerHTML = `<span>${when(r.first[0])}</span><span class="${cls(chg)}">${fmtUsd(r.first[1])} → ${fmtUsd(r.last[1])} (${fmtPct(chg, 2)})</span><span>${when(r.last[0])}</span>`;
  }
  $$("#ranges button").forEach((b) => b.addEventListener("click", () => {
    state.range = b.dataset.range;
    $$("#ranges button").forEach((x) => x.classList.toggle("on", x === b));
    renderChart();
  }));

  // ---- runs ------------------------------------------------------------------------

  function renderRuns() {
    const s = state.data;
    if (!s) return;
    const q = state.runQuery.toLowerCase();
    const rows = s.runs.filter((r) => (state.runFilter === "all" || r.kind === state.runFilter)
      && (!q || `${r.name} ${r.strategy || ""} ${r.package || ""}`.toLowerCase().includes(q)));
    $("#runs").innerHTML = rows.length ? rows.map(runItem).join("") : `<div class="empty">Aucun run ne correspond</div>`;
  }
  $("#run-search").addEventListener("input", (e) => { state.runQuery = e.target.value; renderRuns(); });
  $$("#run-filters button").forEach((b) => b.addEventListener("click", () => {
    state.runFilter = b.dataset.f;
    $$("#run-filters button").forEach((x) => x.classList.toggle("on", x === b));
    renderRuns();
  }));

  function findRun(name) {
    for (const r of state.data?.runs || []) {
      if (r.name === name) return r;
      if (r.folds) for (const f of r.folds) if (f.name === name) return f;
    }
    return null;
  }

  async function openRun(name) {
    let r = findRun(name);
    if (!r) {
      await refreshState();
      r = findRun(name);
      if (!r) throw new Error(`aucun run nommé ${name}`);
    }
    if (r.folds) return openWalkforward(r);
    const head = `<div class="sheet-head">${runIcon(r)}<div><h2>${esc(r.name)}</h2><div class="sub">${esc(r.strategy || r.package || "")}${r.copy ? " · copie " + short(r.copy) : ""}${r.network ? " · " + esc(r.network) : ""}${r.window ? " · " + win(r.window) : ""}</div></div></div>`;
    openSheet("run", name, `${head}<div class="empty">Chargement…</div>`);
    const [eq, rep, evs] = await Promise.all([
      api(`/api/runs/${name}/equity`),
      api(`/api/runs/${name}/report`),
      api(`/api/runs/${name}/events?limit=60`),
    ]);
    if (!state.sheet || state.sheet.id !== name) return;
    const m = rep.metrics;
    const initial = eq.length ? eq[0][1] : null, last = eq.length ? eq[eq.length - 1][1] : null;
    const ret = initial ? ((last - initial) / initial) * 100 : null;
    state.trades = rep.trades;
    const trades = rep.trades.slice(0, 50).map((t, i) => `<tr class="tap" data-card-trade="${i}">
      <td>${coinInline(t.asset)}<br><span class="event when">${when(t.closed_ms)}</span></td>
      <td>${esc(t.direction)} ${t.leverage ? t.leverage + "x" : ""}<br><span class="event when">${esc(t.reason || "")}</span></td>
      <td class="r ${cls(t.pnl_usd)}">${fmtUsd(t.pnl_usd, 2)}<br><span class="event when ${cls(t.roe_pct)}">${fmtPct(t.roe_pct)}</span></td>
    </tr>`).join("");
    const events = evs.map((e) => `<div class="event"><div><b>${esc(e.kind)}</b> ${e.asset ? coinInline(e.asset) : ""} <span class="pill pill-muted">${esc(e.reason || "")}</span></div>
      <div class="when">${when(e.time_ms)} · ${esc(JSON.stringify(e.payload || {}).slice(0, 160))}</div></div>`).join("");
    const controls = r.kind === "live"
      ? `<section class="btn-row">${r.stop
        ? `<button class="btn" data-act="clear-stop" data-run="${esc(name)}">Annuler le STOP</button>`
        : `<button class="btn btn-red" data-act="stop" data-run="${esc(name)}">Arrêter l’agent</button>`}</section>`
      : "";
    $("#sheet-body").innerHTML = `${head}
      <svg class="sheet-chart" viewBox="0 0 360 120" preserveAspectRatio="none"></svg>
      <section class="stats">
        <div><div class="k">Rendement</div><div class="v ${cls(ret)}" id="live-ret">${fmtPct(ret)}</div></div>
        <div><div class="k">Équité</div><div class="v" id="live-eq">${fmtUsd(last)}</div></div>
        <div><div class="k">Max DD</div><div class="v down">${m ? m.drawdown.max_pct.toFixed(1) + "%" : "—"}</div></div>
        <div><div class="k">Trades</div><div class="v">${m ? m.trades : 0}</div></div>
        <div><div class="k">Taux de gain</div><div class="v">${m ? m.win_rate.toFixed(0) + "%" : "—"}</div></div>
        <div><div class="k">Profit factor</div><div class="v">${m && m.profit_factor != null ? fmtNum(m.profit_factor, 2) : "—"}</div></div>
      </section>
      ${controls}
      ${subtabs("run", [
        ["trades", `Trades (${rep.trades.length})`, trades ? `<table class="t"><tr><th>Actif</th><th>Sens</th><th class="r">PnL</th></tr>${trades}</table>` : `<div class="empty">Aucun trade clôturé</div>`],
        ["events", `Événements (${evs.length})`, events || `<div class="empty">Aucun événement</div>`],
        ["info", "Infos", `<div class="kv">
          <div><span class="k">Type</span><span class="v">${esc(r.kind)}</span></div>
          <div><span class="k">Package</span><span class="v mono">${esc(r.package || "—")}</span></div>
          <div><span class="k">Démarré</span><span class="v">${when(r.started_ms || r.first_ms)}</span></div>
          <div><span class="k">Dernier tick</span><span class="v">${when(r.last_tick_ms)}</span></div>
          <div><span class="k">Points</span><span class="v">${eq.length}</span></div>
          <div><span class="k">Capital initial</span><span class="v">${fmtUsd(initial, 2)}</span></div>
          ${r.interval_s ? `<div><span class="k">Intervalle</span><span class="v">${r.interval_s}s</span></div>` : ""}
        </div>`],
      ])}`;
    drawChart($(".sheet-chart"), eq, "all");
    if (r.kind === "live" && r.alive) {
      // Live run: keep the curve and the two headline numbers moving while the sheet is open.
      state.sheet.poll = setInterval(async () => {
        try {
          const cur = await api(`/api/runs/${name}/equity`);
          if (!state.sheet || state.sheet.id !== name) return;
          drawChart($(".sheet-chart"), cur, "all");
          const a = cur.length ? cur[0][1] : null, z = cur.length ? cur[cur.length - 1][1] : null;
          const pct = a ? ((z - a) / a) * 100 : null;
          const re = $("#live-ret"), eqEl = $("#live-eq");
          if (re) { re.textContent = fmtPct(pct); re.className = `v ${cls(pct)}`; }
          if (eqEl) eqEl.textContent = fmtUsd(z);
        } catch (e) { /* transient */ }
      }, 15000);
    }
  }

  function openWalkforward(r) {
    const folds = r.folds.map((f) => `<button class="item" data-run="${esc(f.name)}">
      <div class="icon">${esc(f.name.split("/").pop().replace("fold", "F"))}</div>
      <div class="main"><div class="title"><span class="name">${f.window ? win(f.window) : esc(f.name)}</span></div><div class="sub">${f.trades ?? 0} trades · DD ${f.max_dd_pct == null ? "—" : f.max_dd_pct.toFixed(1) + "%"} · gain ${f.win_rate == null ? "—" : f.win_rate.toFixed(0) + "%"}</div></div>
      <div class="right"><div class="v ${cls(f.return_pct)}">${fmtPct(f.return_pct)}</div></div>
    </button>`).join("");
    openSheet("wf", r.name, `
      <div class="sheet-head">${runIcon(r)}<div><h2>${esc(r.name)}</h2><div class="sub">${esc(r.strategy || "")} · walk-forward, ${r.folds.length} folds</div></div></div>
      <section class="stats">
        <div><div class="k">Composé</div><div class="v ${cls(r.return_pct)}">${fmtPct(r.return_pct)}</div></div>
        <div><div class="k">Folds gagnants</div><div class="v">${r.profitable_folds}/${r.folds.length}</div></div>
        <div><div class="k">Pire DD</div><div class="v down">${r.max_dd_pct == null ? "—" : r.max_dd_pct.toFixed(1) + "%"}</div></div>
      </section>
      <section><div class="section-label">Folds</div><div class="card list">${folds}</div></section>`);
  }

  document.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]");
    if (act) {
      const a = act.dataset.act;
      try {
        if (a === "stop" || a === "clear-stop") {
          if (a === "stop" && !confirm(`Arrêter ${act.dataset.run} ? Les positions ouvertes restent telles quelles.`)) return;
          await api(`/api/runs/${act.dataset.run}/${a}`, { method: "POST" });
          toast(a === "stop" ? "Fichier STOP écrit" : "STOP annulé");
          await refreshState();
          await openRun(act.dataset.run);
        } else if (a === "kill") {
          await api(`/api/jobs/${act.dataset.job}/kill`, { method: "POST" });
          toast("Job arrêté");
          await openJob(act.dataset.job);
        } else if (a === "launch") {
          openLaunch(act.dataset.pkg, act.dataset.kind || "backtest", act.dataset.copy || "");
        } else if (a === "open-run") {
          await openRun(act.dataset.run);
        } else if (a === "fetch") {
          openFetch();
        }
      } catch (err) {
        toast(err.message);
      }
      return;
    }
    const run = e.target.closest("[data-run]");
    if (run) return openRun(run.dataset.run).catch((err) => toast(err.message));
    const st = e.target.closest("[data-strategy]");
    if (st) return openStrategy(st.dataset.strategy).catch((err) => toast(err.message));
    const tr = e.target.closest("[data-trader]");
    if (tr) return openTrader(tr.dataset.trader).catch((err) => toast(err.message));
    const jb = e.target.closest("[data-job]");
    if (jb) return openJob(jb.dataset.job).catch((err) => toast(err.message));
  });

  // ---- strategies ------------------------------------------------------------------

  async function loadStrategies() {
    if (!state.strategies) $("#strategies").innerHTML = `<div class="empty">Chargement…</div>`;
    state.strategies = await api("/api/strategies");
    renderStrategies();
  }

  function riskPill(risk) {
    const r = String(risk || "").toLowerCase();
    if (!r) return "";
    const k = r.includes("high") || r.includes("aggressive") ? "pill-red" : r.includes("low") || r.includes("conservative") ? "pill-green" : "pill-amber";
    return `<span class="pill ${k}">${esc(risk)}</span>`;
  }
  const isSenpi = (c) => /senpi/i.test(c.root) || Object.keys(c.catalog || {}).length > 0;

  function renderStrategies() {
    const q = state.stratQuery.toLowerCase();
    const rows = state.strategies.filter((c) => {
      if (state.stratFilter === "local" && isSenpi(c)) return false;
      if (state.stratFilter === "senpi" && !isSenpi(c)) return false;
      if (state.stratFilter === "tested" && !c.runs) return false;
      if (!q) return true;
      const hay = `${c.id} ${c.name} ${c.description} ${c.group} ${(c.catalog?.tags || []).join(" ")} ${c.assets.join(" ")} ${c.catalog?.tagline || ""}`.toLowerCase();
      return hay.includes(q);
    });
    $("#strategies").innerHTML = rows.length ? rows.map((c) => `<button class="item" data-strategy="${esc(c.id)}">
      <div class="icon ${c.catalog?.emoji ? "emoji" : ""}">${esc(c.catalog?.emoji || c.name.slice(0, 2).toUpperCase())}</div>
      <div class="main">
        <div class="title"><span class="name">${esc(c.catalog?.name || c.name)}</span>${c.error ? `<span class="pill pill-red">cassé</span>` : riskPill(c.catalog?.risk_level || c.risk)}</div>
        <div class="sub">${esc(c.catalog?.tagline || c.description.split("\n")[0] || c.id)}</div>
      </div>
      <div class="right"><div class="v">${c.leverage ? c.leverage + "x" : ""}</div><div class="s">${c.runs ? c.runs + " run" + (c.runs > 1 ? "s" : "") : esc(c.assets.slice(0, 3).join(" "))}</div></div>
    </button>`).join("") : `<div class="empty">Aucune stratégie ne correspond</div>`;
  }
  $("#strat-search").addEventListener("input", (e) => { state.stratQuery = e.target.value; renderStrategies(); });
  $$("#strat-filters button").forEach((b) => b.addEventListener("click", () => {
    state.stratFilter = b.dataset.f;
    $$("#strat-filters button").forEach((x) => x.classList.toggle("on", x === b));
    renderStrategies();
  }));

  async function openStrategy(id) {
    openSheet("strategy", id, `<div class="empty">Chargement…</div>`);
    const c = await api(`/api/strategies/${id}`);
    if (!state.sheet || state.sheet.id !== id) return;
    const cat = c.catalog || {};
    const tags = [
      riskPill(cat.risk_level || c.risk),
      c.leverage ? `<span class="pill pill-muted">${c.leverage}x</span>` : "",
      c.slots ? `<span class="pill pill-muted">${c.slots} slot${c.slots > 1 ? "s" : ""}</span>` : "",
      c.margin_pct != null ? `<span class="pill pill-muted">${c.margin_pct}% de marge</span>` : "",
      cat.tier ? `<span class="pill pill-blue">${esc(cat.tier)}</span>` : "",
      ...(cat.tags || []).map((t) => `<span class="pill pill-muted">${esc(t)}</span>`),
    ].join("");
    const scanners = c.scanners.map((s) => `<div class="event"><b>${esc(s.name)}</b> <span class="pill pill-muted">${esc(s.type)}</span> <span class="when">toutes les ${s.interval_s}s</span>
      <div class="when mono">${esc(JSON.stringify(s.inputs))}</div></div>`).join("");
    const runs = c.run_list.length ? `<div class="card list">${c.run_list.map(runItem).join("")}</div>` : `<div class="empty">Pas encore testée</div>`;
    $("#sheet-body").innerHTML = `
      <div class="sheet-head"><div class="icon">${esc(cat.emoji || "📦")}</div><div><h2>${esc(cat.name || c.name)}</h2><div class="sub">${esc(c.id)}${c.version ? " · v" + esc(c.version) : ""}${c.group ? " · " + esc(c.group) : ""}</div></div></div>
      ${c.error ? `<div class="pill pill-red">${esc(c.error)}</div>` : ""}
      <div class="tags">${tags}</div>
      ${cat.tagline ? `<p style="margin-top:10px"><b>${esc(cat.tagline)}</b></p>` : ""}
      <section class="btn-row">
        <button class="btn" data-act="launch" data-pkg="${esc(c.id)}" data-kind="validate">Valider</button>
        <button class="btn btn-dark" data-act="launch" data-pkg="${esc(c.id)}" data-kind="backtest">Backtest</button>
        <button class="btn" data-act="launch" data-pkg="${esc(c.id)}" data-kind="walkforward">Walk-fwd</button>
        <button class="btn btn-primary" data-act="launch" data-pkg="${esc(c.id)}" data-kind="run">Passer en live</button>
      </section>
      ${subtabs("strategy", [
        ["about", "À propos", `<p>${esc(c.description || "Pas de description.").replace(/\n/g, "<br>")}</p>
          ${c.assets.length ? `<div class="section-label" style="margin-top:12px">Actifs</div><div class="tags">${c.assets.map((a) => `<span class="pill pill-muted">${esc(a)}</span>`).join("")}</div>` : ""}
          <div class="section-label" style="margin-top:12px">Scanners</div>${scanners || `<div class="empty">Aucun</div>`}`],
        ["runs", `Runs (${c.run_list.length})`, runs],
        ["recipe", "Recette", `<pre class="yaml">${esc(c.runtime_yaml)}</pre>`],
      ])}`;
  }

  // ---- jobs: launch forms ----------------------------------------------------------

  const today = () => new Date().toISOString().slice(0, 10);
  const daysAgo = (n) => new Date(Date.now() - n * 86400e3).toISOString().slice(0, 10);

  function openLaunch(pkg, kind, copy) {
    const s = state.settings || {};
    const mainnet = (state.data?.network || s.network) === "mainnet";
    const field = (name, label, type, value, extra = "") => `<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
    let body = "";
    if (kind === "validate") {
      body = field("assets", "Actifs (optionnel, séparés par des espaces)", "text", "");
    } else if (kind === "backtest" || kind === "walkforward") {
      body = `<div class="grid2">${field("start", "Début", "date", daysAgo(180))}${field("end", "Fin", "date", today())}</div>
        <div class="grid2">${field("cash", "Capital ($)", "number", 1000, 'min="10" step="1"')}${field("leverage", "Levier", "number", "", `min="1" max="${s.max_leverage || 10}" placeholder="recette"`)}</div>
        <div class="grid2">${field("step_hours", "Pas (heures)", "number", 1, 'min="1" max="24"')}${kind === "walkforward" ? field("folds", "Folds", "number", 4, 'min="2" max="12"') : field("assets", "Actifs (optionnel)", "text", "")}</div>
        ${field("out", "Nom du run (optionnel)", "text", "")}`;
    } else {
      body = `${field("name", "Nom du run", "text", copy ? "copy-live" : "live")}
        ${field("interval", "Intervalle de tick (s)", "number", 60, 'min="5" max="3600"')}
        ${field("copy", "Trader à copier (optionnel, 0x…)", "text", copy)}
        <div class="grid2">${field("poll", "Poll copie (s)", "number", 60, 'min="30" max="3600"')}${field("budget", "Budget ($, vide = tout le compte)", "number", "", 'min="10"')}</div>
        ${mainnet ? `<label class="check"><input type="checkbox" name="accept_real_money"> J’accepte de trader de l’argent réel sur mainnet</label>` : ""}`;
    }
    openSheet("launch", `${kind}:${pkg}`, `
      <div class="sheet-head"><div class="icon">${ICONS[kind]}</div><div><h2>${esc(TITLES[kind])}</h2><div class="sub">${esc(pkg)} · ${mainnet ? "MAINNET" : "testnet"}</div></div></div>
      ${kind === "run" && mainnet ? `<div class="pill pill-red">Argent réel : ceci lance un agent live sur mainnet.</div>` : ""}
      <form id="launch" class="form" data-kind="${esc(kind)}" data-pkg="${esc(pkg)}">${body}
        <button class="btn btn-dark btn-block" type="submit">Lancer · ${esc(TITLES[kind])}</button></form>`);
  }

  function openFetch() {
    openSheet("fetch", "fetch", `
      <div class="sheet-head"><div class="icon">${ICONS.fetch}</div><div><h2>Télécharger des données</h2><div class="sub">Bougies vers le cache Parquet</div></div></div>
      <form id="launch" class="form" data-kind="fetch">
        <label>Actifs<input name="assets" type="text" value="BTC ETH SOL"></label>
        <div class="grid2"><label>Intervalles<input name="intervals" type="text" value="1h 4h"></label><label>Depuis<input name="since" type="date" value="${daysAgo(365)}"></label></div>
        <label>Source<select name="source"><option value="">par défaut</option><option>hyperliquid</option><option>binance</option><option>both</option></select></label>
        <button class="btn btn-dark btn-block" type="submit">Télécharger</button></form>`);
  }

  $("#sheet-body").addEventListener("submit", async (e) => {
    const f = e.target.closest("#launch");
    if (!f) return;
    e.preventDefault();
    const kind = f.dataset.kind;
    const params = {};
    if (f.dataset.pkg) params.package = f.dataset.pkg;
    for (const el of f.elements) {
      if (!el.name) continue;
      if (el.type === "checkbox") params[el.name] = el.checked;
      else if (el.value !== "") params[el.name] = el.type === "number" ? Number(el.value) : el.value;
    }
    const btn = $("button[type=submit]", f);
    btn.disabled = true;
    try {
      const job = await api("/api/jobs", { method: "POST", body: { kind, params } });
      toast(`Job lancé : ${job.label}`);
      await loadJobs();
      await openJob(job.id);
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
    }
  });

  // ---- jobs: list + detail ---------------------------------------------------------

  async function loadJobs() {
    state.jobs = await api("/api/jobs");
    renderHeader();
    if (state.view === "more") renderJobs();
  }
  function jobPill(j) {
    return j.running ? `<span class="pill pill-blue">en cours</span>` : j.returncode === 0 ? `<span class="pill pill-green">terminé</span>` : `<span class="pill pill-red">code ${j.returncode}</span>`;
  }
  function renderJobs() {
    $("#jobs").innerHTML = state.jobs.length ? state.jobs.map((j) => `<button class="item" data-job="${esc(j.id)}">
      <div class="icon">${esc(j.kind.slice(0, 3).toUpperCase())}</div>
      <div class="main"><div class="title"><span class="name">${esc(j.label)}</span>${jobPill(j)}</div><div class="sub">${esc(j.run || j.argv.slice(1).join(" "))}</div></div>
      <div class="right"><div class="s">${ago(j.started_ms)}</div><div class="s">${dur(j.started_ms, j.ended_ms || Date.now())}</div></div>
    </button>`).join("") : `<div class="empty">Aucun job — valide ou backteste une stratégie</div>`;
  }

  async function openJob(id) {
    const render = async () => {
      const j = await api(`/api/jobs/${id}?lines=300`);
      if (!state.sheet || state.sheet.id !== id) return;
      const pre = $("#sheet-body pre.log");
      const atBottom = !pre || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
      const runName = j.run || "";
      $("#sheet-body").innerHTML = `
        <div class="sheet-head"><div class="icon">${ICONS[j.kind] || "⚙️"}</div><div><h2>${esc(j.label)} ${jobPill(j)}</h2><div class="sub">${when(j.started_ms)} · ${dur(j.started_ms, j.ended_ms || Date.now())}</div></div></div>
        <div class="mono" style="margin-bottom:10px">hl-agent ${esc(j.argv.join(" "))}</div>
        <pre class="log">${esc(j.log || "(pas encore de sortie)")}</pre>
        <section class="btn-row">
          ${j.running ? `<button class="btn btn-red" data-act="kill" data-job="${esc(j.id)}">Arrêter le job</button>` : ""}
          ${!j.running && runName && j.kind !== "fetch" && j.kind !== "validate" ? `<button class="btn" data-act="open-run" data-run="${esc(runName)}">Ouvrir le run</button>` : ""}
        </section>`;
      const p = $("#sheet-body pre.log");
      if (atBottom) p.scrollTop = p.scrollHeight;
      if (!j.running && state.sheet.poll) {
        clearInterval(state.sheet.poll);
        state.sheet.poll = null;
        loadJobs().catch(() => {});
        refreshState().catch(() => {});
      }
    };
    openSheet("job", id, `<div class="empty">Chargement…</div>`);
    await render();
    if (state.sheet && state.sheet.id === id) state.sheet.poll = setInterval(() => render().catch(() => {}), 2000);
  }

  // ---- traders ---------------------------------------------------------------------

  async function loadTraders(refresh) {
    state.traders = await api(`/api/traders${refresh === true ? "?refresh=1" : ""}`);
    const t = state.traders;
    $("#traders-hint").textContent = t.error ? `Erreur : ${t.error}` : t.refreshing ? "Mise à jour du classement…" : `Mis à jour ${ago(t.updated_ms)} · ${t.rows.length} traders copiables`;
    $("#traders").innerHTML = t.rows.length ? t.rows.map((r) => `<button class="item" data-trader="${esc(r.address)}">
      <div class="icon">${esc((r.name || r.address.slice(2, 4)).slice(0, 2).toUpperCase())}</div>
      <div class="main"><div class="title"><span class="name">${esc(r.name || short(r.address))}</span><span class="pill ${r.fit === "good" ? "pill-green" : r.fit === "ok" ? "pill-amber" : "pill-muted"}">${esc(r.fit)}</span></div>
      <div class="sub">${fmtUsd(r.equity)} · ${r.positions} pos · ${r.opens}/${r.lines} copiables · min ${fmtUsd(r.min_budget)}</div></div>
      <div class="right"><div class="v ${cls(r.roi_30d)}">${fmtPct(r.roi_30d)}</div><div class="s">30 j · ${fmtPct(r.roi_7d)} 7 j</div></div>
    </button>`).join("") : `<div class="empty">${t.refreshing ? "Mise à jour…" : "Aucun trader — appuie sur Actualiser"}</div>`;
    if (t.refreshing) setTimeout(() => state.view === "traders" && loadTraders().catch(() => {}), 3000);
  }
  $("#refresh-traders").addEventListener("click", () => loadTraders(true).catch((e) => toast(e.message)));

  async function openTrader(addr) {
    const row = (state.traders?.rows || []).find((r) => r.address === addr) || { address: addr };
    openSheet("trader", addr, `<div class="empty">Chargement…</div>`);
    const budget = Math.max(100, Math.floor(state.data?.account?.value || 100));
    const m = await api(`/api/mirror/${addr}?budget=${budget}`);
    if (!state.sheet || state.sheet.id !== addr) return;
    const planHtml = (p) => `<div class="stats" style="margin-bottom:8px">
        <div><div class="k">Ouvertures</div><div class="v">${p.opens}/${p.lines.length}</div></div>
        <div><div class="k">Marge</div><div class="v">${fmtUsd(p.margin_committed)}</div></div>
        <div><div class="k">Budget min</div><div class="v">${fmtUsd(p.min_budget)}</div></div>
      </div>
      <table class="t"><tr><th>Actif</th><th>Levier</th><th class="r">Marge</th><th class="r">Verdict</th></tr>
      ${p.lines.map((l) => `<tr><td>${coinInline(l.asset)} <span class="pill ${l.direction === "LONG" ? "pill-green" : "pill-red"}">${l.direction === "LONG" ? "L" : "S"}</span></td><td>${l.leverage}x <span class="event when">(og ${l.og_leverage}x)</span></td><td class="r">${fmtUsd(l.margin)}</td><td class="r">${l.verdict === "open" ? `<span class="pill pill-green">ouvrir</span>` : `<span class="pill pill-muted">${esc(l.verdict)}</span>`}</td></tr>`).join("")}
      </table>`;
    const keys = Object.keys(m.plans).sort((a, b) => Number(b) - Number(a));
    $("#sheet-body").innerHTML = `
      <div class="sheet-head"><div class="icon">${esc((row.name || addr.slice(2, 4)).slice(0, 2).toUpperCase())}</div><div><h2>${esc(row.name || short(addr))}</h2><div class="sub mono">${esc(addr)}</div></div></div>
      <section class="stats">
        <div><div class="k">Équité</div><div class="v">${fmtUsd(m.equity)}</div></div>
        <div><div class="k">ROI 30 j</div><div class="v ${cls(row.roi_30d)}">${fmtPct(row.roi_30d)}</div></div>
        <div><div class="k">PnL 30 j</div><div class="v ${cls(row.pnl_30d)}">${fmtUsd(row.pnl_30d)}</div></div>
      </section>
      ${row.flags?.length ? `<div class="tags" style="margin-top:8px">${row.flags.map((f) => `<span class="pill pill-amber">${esc(f)}</span>`).join("")}</div>` : ""}
      <section class="btn-row">
        <button class="btn btn-primary" data-act="launch" data-pkg="copy" data-kind="run" data-copy="${esc(addr)}">Copier ce trader</button>
        <a class="btn" href="https://app.hyperliquid.xyz/explorer/address/${esc(addr)}" target="_blank" rel="noopener">Explorateur</a>
      </section>
      ${subtabs("trader", keys.map((k) => [k, `Miroir à ${fmtUsd(Number(k))}`, planHtml(m.plans[k])]))}`;
  }

  // ---- more ------------------------------------------------------------------------

  async function loadMore() {
    const [jobs, cache, settings] = await Promise.all([api("/api/jobs"), api("/api/data"), api("/api/settings")]);
    state.jobs = jobs;
    state.cache = cache;
    state.settings = settings;
    renderHeader();
    renderJobs();
    const rows = cache.series.map((s) => `<tr><td>${esc(s.asset)}</td><td>${esc(s.interval)}</td><td class="r">${Number(s.bars).toLocaleString()}</td><td class="r">${date(s.first_ms)}<br><span class="event when">${date(s.last_ms)}</span></td></tr>`).join("");
    $("#data").innerHTML = `<div class="hint" style="margin-left:0">${cache.series.length} séries · ${cache.instruments} instruments · <span class="mono">${esc(cache.cache_dir)}</span></div>
      ${rows ? `<table class="t"><tr><th>Actif</th><th>Intervalle</th><th class="r">Bougies</th><th class="r">Période</th></tr>${rows}</table>` : `<div class="empty">Cache vide — télécharge des bougies</div>`}`;
    $("#agent-key").textContent = state.data?.agent_key || "(inconnu)";
    const kv = [
      ["Réseau", settings.network],
      ["Portefeuille", short(settings.address)],
      ["Levier max", `${settings.max_leverage}x`],
      ["Notionnel min", settings.min_notional_usd != null ? fmtUsd(settings.min_notional_usd) : "—"],
      ["Frais taker", settings.taker_pct != null ? `${settings.taker_pct}%` : "—"],
      ["Dossier runs", settings.runs_dir],
      ["Dossier cache", settings.cache_dir],
      ["Dossiers stratégies", (settings.strategy_dirs || []).join(", ")],
      ["Fichier settings", settings.settings_path || "défauts"],
      ["Web", `${settings.web_host || ""}:${settings.web_port || ""}`],
    ];
    $("#settings").innerHTML = kv.map(([k, v]) => `<div><span class="k">${esc(k)}</span><span class="v" title="${esc(v)}">${esc(v)}</span></div>`).join("");
    $("#auth-hint").textContent = settings.auth ? "Authentification par token active : les runs live peuvent être lancés depuis cette page." : "Aucun token web : la page est ouverte et le lancement de runs live depuis la page est désactivé.";
  }
  $("#new-fetch").addEventListener("click", openFetch);
  $("#login").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/login", { method: "POST", body: { token: $("#token").value } });
      $("#token").value = "";
      toast("Connecté");
      await loadMore();
      await refreshState();
    } catch (err) {
      toast(err.message);
    }
  });

  // ---- boot ------------------------------------------------------------------------

  async function refresh() {
    try {
      await refreshState();
      if (state.jobs.some((j) => j.running) || state.view === "more") await loadJobs();
    } catch (e) {
      /* toast shown by api() on 401 */
    }
  }
  refresh();
  loadJobs().catch(() => {});
  setInterval(refresh, 15000);
  document.addEventListener("visibilitychange", () => !document.hidden && refresh());

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
