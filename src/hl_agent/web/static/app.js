/* hl-agent dashboard: the whole project on one page (home, strategies, runs, traders, more). */
(() => {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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
    subtab: {},
  };

  // ---- formatting ----------------------------------------------------------------

  const fmtUsd = (v, d) => {
    if (v == null || Number.isNaN(v)) return "—";
    const n = Number(v);
    const digits = d ?? (Math.abs(n) >= 1000 ? 0 : 2);
    return (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  };
  const fmtPct = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(d)}%`);
  const fmtNum = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d }));
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const ago = (ms) => {
    if (!ms) return "—";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const date = (ms) => (ms ? new Date(ms).toISOString().slice(0, 10) : "—");
  const when = (ms) => (ms ? new Date(ms).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
  const win = (w) => (w && w.length === 2 ? `${date(w[0])} → ${date(w[1])}` : "");
  const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");
  const dur = (a, b) => {
    if (!a || !b) return "";
    const s = Math.round((b - a) / 1000);
    return s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;
  };
  const ICONS = { fetch: "⬇️", validate: "🔍", backtest: "📈", walkforward: "🧪", run: "🚀" };
  const TITLES = { validate: "Validate", backtest: "Backtest", walkforward: "Walk-forward", run: "Go live" };

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
      toast("Sign in with your web token");
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
  document.addEventListener("keydown", (e) => e.key === "Escape" && closeSheet());

  function subtabs(key, tabs) {
    const cur = tabs.some((t) => t[0] === state.subtab[key]) ? state.subtab[key] : tabs[0][0];
    return `<div class="subtabs" data-key="${esc(key)}">${tabs
      .map(([id, label]) => `<button type="button" data-tab="${esc(id)}" class="${id === cur ? "on" : ""}">${esc(label)}</button>`)
      .join("")}</div>${tabs.map(([id, , html]) => `<div data-pane="${esc(id)}" class="${id === cur ? "" : "hidden"}">${html}</div>`).join("")}`;
  }
  $("#sheet-body").addEventListener("click", (e) => {
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
      ? `${r.copy ? "copy " + short(r.copy) : r.strategy || r.package || ""} · ${r.alive ? "tick " + ago(r.last_tick_ms) : "stopped " + ago(r.last_tick_ms)}`
      : r.kind === "walkforward"
        ? `${r.strategy || ""} · ${r.folds.length} folds · ${r.profitable_folds}/${r.folds.length} profitable`
        : `${r.strategy || ""} · ${r.window ? win(r.window) : date(r.first_ms)}`;
    const badge = r.kind === "live"
      ? (r.stop ? `<span class="pill pill-red">STOP</span>` : r.alive ? `<span class="pill pill-green">running</span>` : `<span class="pill pill-muted">idle</span>`)
      : "";
    return `<button class="item" data-run="${esc(r.name)}">
      ${runIcon(r)}
      <div class="main"><div class="title"><span class="name">${esc(r.name)}</span>${badge}</div><div class="sub">${esc(sub)}</div></div>
      <div class="right"><div class="v ${cls(ret)}">${fmtPct(ret)}</div><div class="s">${r.trades ?? 0} trades · dd ${r.max_dd_pct == null ? "—" : r.max_dd_pct.toFixed(1) + "%"}</div></div>
    </button>`;
  }

  async function refreshState() {
    state.data = await api("/api/state");
    renderHeader();
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
    dot.title = key || "no agent key";
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
    delta.textContent = `${fmtUsd(upnl, 2)} unrealised · ${fmtUsd(acct.withdrawable, 2)} free`;

    const pos = $("#positions");
    pos.innerHTML = acct.positions.length
      ? acct.positions.map((p) => `<div class="item">
          <div class="icon">${esc(p.asset.slice(0, 4))}</div>
          <div class="main"><div class="title"><span class="name">${esc(p.asset)}</span><span class="pill ${p.direction === "LONG" ? "pill-green" : "pill-red"}">${p.direction === "LONG" ? "Long" : "Short"} ${p.leverage}x</span></div>
          <div class="sub">${fmtNum(p.size, 4)} @ ${fmtUsd(p.entry, 2)} · now ${fmtUsd(p.price, 2)} · liq ${fmtUsd(p.liquidation, 0)}</div></div>
          <div class="right"><div class="v ${cls(p.upnl)}">${fmtUsd(p.upnl, 2)}</div><div class="s ${cls(p.roe)}">${fmtPct(p.roe)}</div></div>
        </div>`).join("")
      : `<div class="empty">No open position</div>`;

    const live = s.runs.filter((r) => r.kind === "live");
    $("#live-runs").innerHTML = live.length ? live.map(runItem).join("") : `<div class="empty">No live run — launch one from a strategy</div>`;
    const recent = s.runs.filter((r) => r.kind !== "live").slice(0, 5);
    $("#recent-runs").innerHTML = recent.length ? recent.map(runItem).join("") : `<div class="empty">No backtest yet</div>`;

    const target = live[0] || s.runs[0];
    $("#chart-title").textContent = target ? `${target.kind === "live" ? "Acc. value" : "Equity"} · ${target.name}` : "Acc. value";
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
      foot.innerHTML = `<span>no equity yet</span>`;
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
    $("#runs").innerHTML = rows.length ? rows.map(runItem).join("") : `<div class="empty">No matching run</div>`;
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
      if (!r) throw new Error(`no run named ${name}`);
    }
    if (r.folds) return openWalkforward(r);
    const head = `<div class="sheet-head">${runIcon(r)}<div><h2>${esc(r.name)}</h2><div class="sub">${esc(r.strategy || r.package || "")}${r.copy ? " · copy " + short(r.copy) : ""}${r.network ? " · " + esc(r.network) : ""}${r.window ? " · " + win(r.window) : ""}</div></div></div>`;
    openSheet("run", name, `${head}<div class="empty">Loading…</div>`);
    const [eq, rep, evs] = await Promise.all([
      api(`/api/runs/${name}/equity`),
      api(`/api/runs/${name}/report`),
      api(`/api/runs/${name}/events?limit=60`),
    ]);
    if (!state.sheet || state.sheet.id !== name) return;
    const m = rep.metrics;
    const initial = eq.length ? eq[0][1] : null, last = eq.length ? eq[eq.length - 1][1] : null;
    const ret = initial ? ((last - initial) / initial) * 100 : null;
    const trades = rep.trades.slice(0, 50).map((t) => `<tr>
      <td>${esc(t.asset)}<br><span class="event when">${when(t.closed_ms)}</span></td>
      <td>${esc(t.direction)} ${t.leverage ? t.leverage + "x" : ""}<br><span class="event when">${esc(t.reason || "")}</span></td>
      <td class="r ${cls(t.pnl_usd)}">${fmtUsd(t.pnl_usd, 2)}<br><span class="event when ${cls(t.roe_pct)}">${fmtPct(t.roe_pct)}</span></td>
    </tr>`).join("");
    const events = evs.map((e) => `<div class="event"><div><b>${esc(e.kind)}</b> ${esc(e.asset || "")} <span class="pill pill-muted">${esc(e.reason || "")}</span></div>
      <div class="when">${when(e.time_ms)} · ${esc(JSON.stringify(e.payload || {}).slice(0, 160))}</div></div>`).join("");
    const controls = r.kind === "live"
      ? `<section class="btn-row">${r.stop
        ? `<button class="btn" data-act="clear-stop" data-run="${esc(name)}">Clear STOP</button>`
        : `<button class="btn btn-red" data-act="stop" data-run="${esc(name)}">Stop agent</button>`}</section>`
      : "";
    $("#sheet-body").innerHTML = `${head}
      <svg class="sheet-chart" viewBox="0 0 360 120" preserveAspectRatio="none"></svg>
      <section class="stats">
        <div><div class="k">Return</div><div class="v ${cls(ret)}">${fmtPct(ret)}</div></div>
        <div><div class="k">Equity</div><div class="v">${fmtUsd(last)}</div></div>
        <div><div class="k">Max DD</div><div class="v down">${m ? m.drawdown.max_pct.toFixed(1) + "%" : "—"}</div></div>
        <div><div class="k">Trades</div><div class="v">${m ? m.trades : 0}</div></div>
        <div><div class="k">Win rate</div><div class="v">${m ? m.win_rate.toFixed(0) + "%" : "—"}</div></div>
        <div><div class="k">Profit factor</div><div class="v">${m && m.profit_factor != null ? fmtNum(m.profit_factor, 2) : "—"}</div></div>
      </section>
      ${controls}
      ${subtabs("run", [
        ["trades", `Trades (${rep.trades.length})`, trades ? `<table class="t"><tr><th>Asset</th><th>Side</th><th class="r">PnL</th></tr>${trades}</table>` : `<div class="empty">No closed trade</div>`],
        ["events", `Events (${evs.length})`, events || `<div class="empty">No event</div>`],
        ["info", "Info", `<div class="kv">
          <div><span class="k">Kind</span><span class="v">${esc(r.kind)}</span></div>
          <div><span class="k">Package</span><span class="v mono">${esc(r.package || "—")}</span></div>
          <div><span class="k">Started</span><span class="v">${when(r.started_ms || r.first_ms)}</span></div>
          <div><span class="k">Last tick</span><span class="v">${when(r.last_tick_ms)}</span></div>
          <div><span class="k">Points</span><span class="v">${eq.length}</span></div>
          <div><span class="k">Initial</span><span class="v">${fmtUsd(initial, 2)}</span></div>
          ${r.interval_s ? `<div><span class="k">Interval</span><span class="v">${r.interval_s}s</span></div>` : ""}
        </div>`],
      ])}`;
    drawChart($(".sheet-chart"), eq, "all");
  }

  function openWalkforward(r) {
    const folds = r.folds.map((f) => `<button class="item" data-run="${esc(f.name)}">
      <div class="icon">${esc(f.name.split("/").pop().replace("fold", "F"))}</div>
      <div class="main"><div class="title"><span class="name">${f.window ? win(f.window) : esc(f.name)}</span></div><div class="sub">${f.trades ?? 0} trades · dd ${f.max_dd_pct == null ? "—" : f.max_dd_pct.toFixed(1) + "%"} · win ${f.win_rate == null ? "—" : f.win_rate.toFixed(0) + "%"}</div></div>
      <div class="right"><div class="v ${cls(f.return_pct)}">${fmtPct(f.return_pct)}</div></div>
    </button>`).join("");
    openSheet("wf", r.name, `
      <div class="sheet-head">${runIcon(r)}<div><h2>${esc(r.name)}</h2><div class="sub">${esc(r.strategy || "")} · walk-forward, ${r.folds.length} folds</div></div></div>
      <section class="stats">
        <div><div class="k">Compounded</div><div class="v ${cls(r.return_pct)}">${fmtPct(r.return_pct)}</div></div>
        <div><div class="k">Profitable</div><div class="v">${r.profitable_folds}/${r.folds.length}</div></div>
        <div><div class="k">Worst DD</div><div class="v down">${r.max_dd_pct == null ? "—" : r.max_dd_pct.toFixed(1) + "%"}</div></div>
      </section>
      <section><div class="section-label">Folds</div><div class="card list">${folds}</div></section>`);
  }

  document.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]");
    if (act) {
      const a = act.dataset.act;
      try {
        if (a === "stop" || a === "clear-stop") {
          if (a === "stop" && !confirm(`Stop ${act.dataset.run}? Open positions are left as they are.`)) return;
          await api(`/api/runs/${act.dataset.run}/${a}`, { method: "POST" });
          toast(a === "stop" ? "STOP file written" : "STOP cleared");
          await refreshState();
          await openRun(act.dataset.run);
        } else if (a === "kill") {
          await api(`/api/jobs/${act.dataset.job}/kill`, { method: "POST" });
          toast("Job stopped");
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
    if (!state.strategies) $("#strategies").innerHTML = `<div class="empty">Loading…</div>`;
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
        <div class="title"><span class="name">${esc(c.catalog?.name || c.name)}</span>${c.error ? `<span class="pill pill-red">broken</span>` : riskPill(c.catalog?.risk_level || c.risk)}</div>
        <div class="sub">${esc(c.catalog?.tagline || c.description.split("\n")[0] || c.id)}</div>
      </div>
      <div class="right"><div class="v">${c.leverage ? c.leverage + "x" : ""}</div><div class="s">${c.runs ? c.runs + " run" + (c.runs > 1 ? "s" : "") : esc(c.assets.slice(0, 3).join(" "))}</div></div>
    </button>`).join("") : `<div class="empty">No strategy matches</div>`;
  }
  $("#strat-search").addEventListener("input", (e) => { state.stratQuery = e.target.value; renderStrategies(); });
  $$("#strat-filters button").forEach((b) => b.addEventListener("click", () => {
    state.stratFilter = b.dataset.f;
    $$("#strat-filters button").forEach((x) => x.classList.toggle("on", x === b));
    renderStrategies();
  }));

  async function openStrategy(id) {
    openSheet("strategy", id, `<div class="empty">Loading…</div>`);
    const c = await api(`/api/strategies/${id}`);
    if (!state.sheet || state.sheet.id !== id) return;
    const cat = c.catalog || {};
    const tags = [
      riskPill(cat.risk_level || c.risk),
      c.leverage ? `<span class="pill pill-muted">${c.leverage}x</span>` : "",
      c.slots ? `<span class="pill pill-muted">${c.slots} slot${c.slots > 1 ? "s" : ""}</span>` : "",
      c.margin_pct != null ? `<span class="pill pill-muted">${c.margin_pct}% margin</span>` : "",
      cat.tier ? `<span class="pill pill-blue">${esc(cat.tier)}</span>` : "",
      ...(cat.tags || []).map((t) => `<span class="pill pill-muted">${esc(t)}</span>`),
    ].join("");
    const scanners = c.scanners.map((s) => `<div class="event"><b>${esc(s.name)}</b> <span class="pill pill-muted">${esc(s.type)}</span> <span class="when">every ${s.interval_s}s</span>
      <div class="when mono">${esc(JSON.stringify(s.inputs))}</div></div>`).join("");
    const runs = c.run_list.length ? `<div class="card list">${c.run_list.map(runItem).join("")}</div>` : `<div class="empty">Not tested yet</div>`;
    $("#sheet-body").innerHTML = `
      <div class="sheet-head"><div class="icon">${esc(cat.emoji || "📦")}</div><div><h2>${esc(cat.name || c.name)}</h2><div class="sub">${esc(c.id)}${c.version ? " · v" + esc(c.version) : ""}${c.group ? " · " + esc(c.group) : ""}</div></div></div>
      ${c.error ? `<div class="pill pill-red">${esc(c.error)}</div>` : ""}
      <div class="tags">${tags}</div>
      ${cat.tagline ? `<p style="margin-top:10px"><b>${esc(cat.tagline)}</b></p>` : ""}
      <section class="btn-row">
        <button class="btn" data-act="launch" data-pkg="${esc(c.id)}" data-kind="validate">Validate</button>
        <button class="btn btn-dark" data-act="launch" data-pkg="${esc(c.id)}" data-kind="backtest">Backtest</button>
        <button class="btn" data-act="launch" data-pkg="${esc(c.id)}" data-kind="walkforward">Walk-fwd</button>
        <button class="btn btn-primary" data-act="launch" data-pkg="${esc(c.id)}" data-kind="run">Go live</button>
      </section>
      ${subtabs("strategy", [
        ["about", "About", `<p>${esc(c.description || "No description.").replace(/\n/g, "<br>")}</p>
          ${c.assets.length ? `<div class="section-label" style="margin-top:12px">Assets</div><div class="tags">${c.assets.map((a) => `<span class="pill pill-muted">${esc(a)}</span>`).join("")}</div>` : ""}
          <div class="section-label" style="margin-top:12px">Scanners</div>${scanners || `<div class="empty">None</div>`}`],
        ["runs", `Runs (${c.run_list.length})`, runs],
        ["recipe", "Recipe", `<pre class="yaml">${esc(c.runtime_yaml)}</pre>`],
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
      body = field("assets", "Assets (optional, space separated)", "text", "");
    } else if (kind === "backtest" || kind === "walkforward") {
      body = `<div class="grid2">${field("start", "Start", "date", daysAgo(180))}${field("end", "End", "date", today())}</div>
        <div class="grid2">${field("cash", "Cash ($)", "number", 1000, 'min="10" step="1"')}${field("leverage", "Leverage", "number", "", `min="1" max="${s.max_leverage || 10}" placeholder="recipe"`)}</div>
        <div class="grid2">${field("step_hours", "Step (hours)", "number", 1, 'min="1" max="24"')}${kind === "walkforward" ? field("folds", "Folds", "number", 4, 'min="2" max="12"') : field("assets", "Assets (optional)", "text", "")}</div>
        ${field("out", "Run name (optional)", "text", "")}`;
    } else {
      body = `${field("name", "Run name", "text", copy ? "copy-live" : "live")}
        ${field("interval", "Tick interval (s)", "number", 60, 'min="5" max="3600"')}
        ${field("copy", "Copy trader (optional 0x…)", "text", copy)}
        <div class="grid2">${field("poll", "Copy poll (s)", "number", 60, 'min="30" max="3600"')}${field("budget", "Budget ($, blank = whole account)", "number", "", 'min="10"')}</div>
        ${mainnet ? `<label class="check"><input type="checkbox" name="accept_real_money"> I accept trading real money on mainnet</label>` : ""}`;
    }
    openSheet("launch", `${kind}:${pkg}`, `
      <div class="sheet-head"><div class="icon">${ICONS[kind]}</div><div><h2>${esc(TITLES[kind])}</h2><div class="sub">${esc(pkg)} · ${mainnet ? "MAINNET" : "testnet"}</div></div></div>
      ${kind === "run" && mainnet ? `<div class="pill pill-red">Real money: this launches a live agent on mainnet.</div>` : ""}
      <form id="launch" class="form" data-kind="${esc(kind)}" data-pkg="${esc(pkg)}">${body}
        <button class="btn btn-dark btn-block" type="submit">Launch ${esc(kind)}</button></form>`);
  }

  function openFetch() {
    openSheet("fetch", "fetch", `
      <div class="sheet-head"><div class="icon">${ICONS.fetch}</div><div><h2>Fetch data</h2><div class="sub">Candles into the Parquet cache</div></div></div>
      <form id="launch" class="form" data-kind="fetch">
        <label>Assets<input name="assets" type="text" value="BTC ETH SOL"></label>
        <div class="grid2"><label>Intervals<input name="intervals" type="text" value="1h 4h"></label><label>Since<input name="since" type="date" value="${daysAgo(365)}"></label></div>
        <label>Source<select name="source"><option value="">default</option><option>hyperliquid</option><option>binance</option><option>both</option></select></label>
        <button class="btn btn-dark btn-block" type="submit">Fetch</button></form>`);
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
      toast(`Job started: ${job.label}`);
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
    return j.running ? `<span class="pill pill-blue">running</span>` : j.returncode === 0 ? `<span class="pill pill-green">done</span>` : `<span class="pill pill-red">exit ${j.returncode}</span>`;
  }
  function renderJobs() {
    $("#jobs").innerHTML = state.jobs.length ? state.jobs.map((j) => `<button class="item" data-job="${esc(j.id)}">
      <div class="icon">${esc(j.kind.slice(0, 3).toUpperCase())}</div>
      <div class="main"><div class="title"><span class="name">${esc(j.label)}</span>${jobPill(j)}</div><div class="sub">${esc(j.run || j.argv.slice(1).join(" "))}</div></div>
      <div class="right"><div class="s">${ago(j.started_ms)}</div><div class="s">${dur(j.started_ms, j.ended_ms || Date.now())}</div></div>
    </button>`).join("") : `<div class="empty">No job yet — validate or backtest a strategy</div>`;
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
        <pre class="log">${esc(j.log || "(no output yet)")}</pre>
        <section class="btn-row">
          ${j.running ? `<button class="btn btn-red" data-act="kill" data-job="${esc(j.id)}">Stop job</button>` : ""}
          ${!j.running && runName && j.kind !== "fetch" && j.kind !== "validate" ? `<button class="btn" data-act="open-run" data-run="${esc(runName)}">Open run</button>` : ""}
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
    openSheet("job", id, `<div class="empty">Loading…</div>`);
    await render();
    if (state.sheet && state.sheet.id === id) state.sheet.poll = setInterval(() => render().catch(() => {}), 2000);
  }

  // ---- traders ---------------------------------------------------------------------

  async function loadTraders(refresh) {
    state.traders = await api(`/api/traders${refresh === true ? "?refresh=1" : ""}`);
    const t = state.traders;
    $("#traders-hint").textContent = t.error ? `Error: ${t.error}` : t.refreshing ? "Refreshing leaderboard…" : `Updated ${ago(t.updated_ms)} · ${t.rows.length} copyable traders`;
    $("#traders").innerHTML = t.rows.length ? t.rows.map((r) => `<button class="item" data-trader="${esc(r.address)}">
      <div class="icon">${esc((r.name || r.address.slice(2, 4)).slice(0, 2).toUpperCase())}</div>
      <div class="main"><div class="title"><span class="name">${esc(r.name || short(r.address))}</span><span class="pill ${r.fit === "good" ? "pill-green" : r.fit === "ok" ? "pill-amber" : "pill-muted"}">${esc(r.fit)}</span></div>
      <div class="sub">${fmtUsd(r.equity)} · ${r.positions} pos · ${r.opens}/${r.lines} mirrorable · min ${fmtUsd(r.min_budget)}</div></div>
      <div class="right"><div class="v ${cls(r.roi_30d)}">${fmtPct(r.roi_30d)}</div><div class="s">30d · ${fmtPct(r.roi_7d)} 7d</div></div>
    </button>`).join("") : `<div class="empty">${t.refreshing ? "Refreshing…" : "No trader yet — tap Refresh"}</div>`;
    if (t.refreshing) setTimeout(() => state.view === "traders" && loadTraders().catch(() => {}), 3000);
  }
  $("#refresh-traders").addEventListener("click", () => loadTraders(true).catch((e) => toast(e.message)));

  async function openTrader(addr) {
    const row = (state.traders?.rows || []).find((r) => r.address === addr) || { address: addr };
    openSheet("trader", addr, `<div class="empty">Loading…</div>`);
    const budget = Math.max(100, Math.floor(state.data?.account?.value || 100));
    const m = await api(`/api/mirror/${addr}?budget=${budget}`);
    if (!state.sheet || state.sheet.id !== addr) return;
    const planHtml = (p) => `<div class="stats" style="margin-bottom:8px">
        <div><div class="k">Opens</div><div class="v">${p.opens}/${p.lines.length}</div></div>
        <div><div class="k">Margin</div><div class="v">${fmtUsd(p.margin_committed)}</div></div>
        <div><div class="k">Min budget</div><div class="v">${fmtUsd(p.min_budget)}</div></div>
      </div>
      <table class="t"><tr><th>Asset</th><th>Lev</th><th class="r">Margin</th><th class="r">Verdict</th></tr>
      ${p.lines.map((l) => `<tr><td>${esc(l.asset)} <span class="pill ${l.direction === "LONG" ? "pill-green" : "pill-red"}">${l.direction === "LONG" ? "L" : "S"}</span></td><td>${l.leverage}x <span class="event when">(og ${l.og_leverage}x)</span></td><td class="r">${fmtUsd(l.margin)}</td><td class="r">${l.verdict === "open" ? `<span class="pill pill-green">open</span>` : `<span class="pill pill-muted">${esc(l.verdict)}</span>`}</td></tr>`).join("")}
      </table>`;
    const keys = Object.keys(m.plans).sort((a, b) => Number(b) - Number(a));
    $("#sheet-body").innerHTML = `
      <div class="sheet-head"><div class="icon">${esc((row.name || addr.slice(2, 4)).slice(0, 2).toUpperCase())}</div><div><h2>${esc(row.name || short(addr))}</h2><div class="sub mono">${esc(addr)}</div></div></div>
      <section class="stats">
        <div><div class="k">Equity</div><div class="v">${fmtUsd(m.equity)}</div></div>
        <div><div class="k">30d ROI</div><div class="v ${cls(row.roi_30d)}">${fmtPct(row.roi_30d)}</div></div>
        <div><div class="k">30d PnL</div><div class="v ${cls(row.pnl_30d)}">${fmtUsd(row.pnl_30d)}</div></div>
      </section>
      ${row.flags?.length ? `<div class="tags" style="margin-top:8px">${row.flags.map((f) => `<span class="pill pill-amber">${esc(f)}</span>`).join("")}</div>` : ""}
      <section class="btn-row">
        <button class="btn btn-primary" data-act="launch" data-pkg="copy" data-kind="run" data-copy="${esc(addr)}">Copy this trader</button>
        <a class="btn" href="https://app.hyperliquid.xyz/explorer/address/${esc(addr)}" target="_blank" rel="noopener">Explorer</a>
      </section>
      ${subtabs("trader", keys.map((k) => [k, `Mirror @ ${fmtUsd(Number(k))}`, planHtml(m.plans[k])]))}`;
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
    $("#data").innerHTML = `<div class="hint" style="margin-left:0">${cache.series.length} series · ${cache.instruments} instruments · <span class="mono">${esc(cache.cache_dir)}</span></div>
      ${rows ? `<table class="t"><tr><th>Asset</th><th>Interval</th><th class="r">Bars</th><th class="r">Window</th></tr>${rows}</table>` : `<div class="empty">Cache empty — fetch some candles</div>`}`;
    $("#agent-key").textContent = state.data?.agent_key || "(unknown)";
    const kv = [
      ["Network", settings.network],
      ["Wallet", short(settings.address)],
      ["Max leverage", `${settings.max_leverage}x`],
      ["Min notional", settings.min_notional_usd != null ? fmtUsd(settings.min_notional_usd) : "—"],
      ["Taker fee", settings.taker_pct != null ? `${settings.taker_pct}%` : "—"],
      ["Runs dir", settings.runs_dir],
      ["Cache dir", settings.cache_dir],
      ["Strategy dirs", (settings.strategy_dirs || []).join(", ")],
      ["Settings file", settings.settings_path || "defaults"],
      ["Web", `${settings.web_host || ""}:${settings.web_port || ""}`],
    ];
    $("#settings").innerHTML = kv.map(([k, v]) => `<div><span class="k">${esc(k)}</span><span class="v" title="${esc(v)}">${esc(v)}</span></div>`).join("");
    $("#auth-hint").textContent = settings.auth ? "Token auth is on. Live runs can be launched from this page." : "No web token set: the page is open, and launching live runs from it is disabled.";
  }
  $("#new-fetch").addEventListener("click", openFetch);
  $("#login").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/login", { method: "POST", body: { token: $("#token").value } });
      $("#token").value = "";
      toast("Signed in");
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
