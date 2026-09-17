/* hl-agent dashboard. Vanilla JS: polls /api/state, draws the equity curve, lists positions,
   runs, traders and events, and exposes the kill switch. */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const fmtUsd = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(v)
      ? "—"
      : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtPct = (v, d = 2) => (v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + v.toFixed(d) + "%");
  const fmtNum = (v, d = 4) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d }));
  const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "—");
  const ago = (ms, now) => {
    if (!ms) return "never";
    const s = Math.max(0, Math.round((now - ms) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  };
  const when = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const sign = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

  const state = { data: null, run: null, range: "all", equity: [], view: "home", timer: null };

  // ---- api ---------------------------------------------------------------------------
  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
    if (res.status === 401) {
      showView("profile");
      toast("Token required");
      throw new Error("unauthorized");
    }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || res.statusText);
    return body;
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.add("hidden"), 2200);
  }

  // ---- views -------------------------------------------------------------------------
  function showView(name) {
    state.view = name;
    closeSheet();
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== "view-" + name));
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === name));
    try { localStorage.setItem("view", name); } catch (_) {}
    if (name === "traders") loadTraders();
    if (name === "activity") loadActivity();
  }
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

  // ---- home --------------------------------------------------------------------------
  function currentRun() {
    const runs = (state.data && state.data.runs) || [];
    const live = runs.filter((r) => r.kind === "live");
    return live.find((r) => r.name === state.run) || live.find((r) => r.alive) || live[0] || null;
  }

  function renderHome(d) {
    const run = currentRun();
    $("#net").textContent = d.network;
    $("#net").className = "pill " + (d.network === "mainnet" ? "pill-green" : "pill-muted");
    const keyOk = /authorised/.test(d.agent_key || "");
    $("#key-dot").className = "dot " + (keyOk ? "dot-green" : /missing/.test(d.agent_key) ? "dot-muted" : "dot-red");
    $("#key-dot").title = d.agent_key;

    const a = d.account;
    $("#balance").textContent = fmtUsd(a.value);
    const base = run && run.initial ? run.initial : null;
    const dl = $("#delta");
    if (base) {
      const diff = a.value - base;
      dl.textContent = `${diff >= 0 ? "+" : "-"}${fmtUsd(Math.abs(diff))} (${fmtPct((diff / base) * 100)}) since ${run.name}`;
      dl.className = "delta " + sign(diff);
    } else {
      dl.textContent = `${fmtUsd(a.withdrawable)} withdrawable · ${fmtUsd(a.margin_used)} in margin`;
      dl.className = "delta muted";
    }

    // positions
    const box = $("#positions");
    box.innerHTML = "";
    if (!a.positions.length) box.appendChild(el("div", "empty", "No open position"));
    for (const p of a.positions) {
      const row = el("div", "row-item");
      const main = el("div", "row-main");
      const title = el("div", "row-title");
      title.appendChild(el("span", null, p.asset));
      title.appendChild(el("span", "badge badge-" + p.direction.toLowerCase(), p.direction));
      main.appendChild(title);
      main.appendChild(el("div", "row-sub", `${fmtNum(p.size)} × ${p.leverage}x · entry ${fmtNum(p.entry, 6)} → ${fmtNum(p.price, 6)}`));
      const end = el("div", "row-end");
      end.appendChild(el("div", "v " + sign(p.upnl), fmtUsd(p.upnl)));
      end.appendChild(el("div", "s " + sign(p.roe), fmtPct(p.roe, 1) + " · " + fmtUsd(p.margin, 0) + " margin"));
      row.append(main, end);
      box.appendChild(row);
    }

    // agent card
    const ag = $("#agent");
    ag.innerHTML = "";
    if (!run) {
      ag.appendChild(el("div", "empty", "No live run in runs/"));
    } else {
      const head = el("div", "agent-head");
      const left = el("div");
      const t = el("div", "agent-title");
      t.appendChild(el("span", "dot " + (run.stop ? "dot-red" : run.alive ? "dot-green" : "dot-amber")));
      t.appendChild(el("span", null, run.name));
      left.appendChild(t);
      const what = run.copy ? `mirroring ${short(run.copy)}` : run.package || "";
      left.appendChild(el("div", "agent-sub", `${what} · ${run.alive ? "live" : "stale"}, last tick ${ago(run.last_tick_ms, d.now_ms)} · ${run.ticks} ticks`));
      head.appendChild(left);
      head.appendChild(el("span", "pill " + (run.stop ? "pill-red" : run.alive ? "pill-green" : "pill-muted"), run.stop ? "STOP" : run.alive ? "Running" : "Stale"));
      ag.appendChild(head);
      const actions = el("div", "agent-actions");
      if (run.stop) {
        const clr = el("button", "btn btn-light", "Clear STOP");
        clr.onclick = () => api(`/api/runs/${run.name}/clear-stop`, { method: "POST" }).then(() => { toast("STOP cleared"); refresh(); });
        actions.appendChild(clr);
        actions.appendChild(el("div", "small muted", "The agent flattens and exits on its next tick; relaunch it to trade again."));
      } else {
        const stop = el("button", "btn btn-red", "Stop agent");
        stop.onclick = () => {
          if (confirm(`Stop ${run.name}? Every open position is closed at market on the next tick.`))
            api(`/api/runs/${run.name}/stop`, { method: "POST" }).then(() => { toast("STOP written"); refresh(); });
        };
        actions.appendChild(stop);
      }
      ag.appendChild(actions);
    }
  }

  // ---- chart -------------------------------------------------------------------------
  async function loadEquity() {
    const run = currentRun();
    if (!run) { state.equity = []; drawChart(); return; }
    state.equity = await api(`/api/runs/${run.name}/equity`);
    drawChart();
  }

  function drawChart() {
    const svg = $("#chart");
    const now = state.data ? state.data.now_ms : Date.now();
    const span = { "1d": 864e5, "7d": 7 * 864e5, all: Infinity }[state.range];
    let pts = state.equity.filter((p) => now - p[0] <= span);
    if (pts.length < 2) pts = state.equity.slice(-2);
    svg.innerHTML = "";
    $("#chart-foot").innerHTML = "";
    if (pts.length < 2) { $("#chart-title").textContent = "Acc. value"; return; }
    const W = 360, H = 140, padY = 8;
    const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
    let lo = Math.min(...pts.map((p) => p[1])), hi = Math.max(...pts.map((p) => p[1]));
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
    const y = (v) => H - padY - ((v - lo) / (hi - lo)) * (H - 2 * padY);
    const up = pts[pts.length - 1][1] >= pts[0][1];
    const color = up ? "var(--green)" : "var(--red)";
    const d = pts.map((p, i) => (i ? "L" : "M") + x(p[0]).toFixed(1) + " " + y(p[1]).toFixed(1)).join(" ");
    const ns = "http://www.w3.org/2000/svg";
    const defs = document.createElementNS(ns, "defs");
    defs.innerHTML = `<linearGradient id="g" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${up ? "#1cca5b" : "#ef4444"}" stop-opacity=".22"/><stop offset="1" stop-color="${up ? "#1cca5b" : "#ef4444"}" stop-opacity="0"/></linearGradient>`;
    svg.appendChild(defs);
    const area = document.createElementNS(ns, "path");
    area.setAttribute("d", `${d} L${W} ${H} L0 ${H} Z`);
    area.setAttribute("fill", "url(#g)");
    const line = document.createElementNS(ns, "path");
    line.setAttribute("d", d);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "2");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    line.setAttribute("stroke-linejoin", "round");
    svg.append(area, line);
    const last = pts[pts.length - 1][1], first = pts[0][1];
    $("#chart-title").textContent = `Acc. value · ${fmtPct(((last - first) / first) * 100)}`;
    const f = $("#chart-foot");
    f.append(el("span", null, when(t0)), el("span", null, `${fmtUsd(lo, 0)} – ${fmtUsd(hi, 0)}`), el("span", null, when(t1)));
  }
  $("#ranges").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.range = b.dataset.range;
    document.querySelectorAll("#ranges button").forEach((x) => x.classList.toggle("on", x === b));
    drawChart();
  });

  // ---- traders -----------------------------------------------------------------------
  async function loadTraders(refreshNow = false) {
    const box = $("#traders");
    const data = await api("/api/traders" + (refreshNow ? "?refresh=1" : ""));
    const meta = $("#traders-meta");
    if (data.error) meta.textContent = "Leaderboard error: " + data.error;
    else if (data.refreshing && !data.rows.length) meta.textContent = "Computing candidates (about 20 s)…";
    else meta.textContent = `7d ROI · 30d ROI · 30d PnL blend · ${data.rows.length} copyable · updated ${ago(data.updated_ms, Date.now())}`;
    box.innerHTML = "";
    if (!data.rows.length) {
      box.appendChild(el("div", "empty", data.refreshing ? "Loading…" : "No candidate"));
      if (data.refreshing) setTimeout(() => state.view === "traders" && loadTraders(), 4000);
      return;
    }
    data.rows.forEach((r, i) => {
      const row = el("div", "row-item tap");
      row.appendChild(el("div", "rank", String(i + 1)));
      const main = el("div", "row-main");
      main.style.flex = "1";
      const title = el("div", "row-title");
      title.appendChild(el("span", "mono", short(r.address)));
      title.appendChild(el("span", "badge badge-" + r.fit, r.fit));
      main.appendChild(title);
      main.appendChild(el("div", "row-sub", `${fmtUsd(r.equity, 0)} · ${r.positions} pos (${r.longs} L) · opens ${r.opens}/${r.lines} at $100 · ${r.seen_in.join(", ")}`));
      const end = el("div", "row-end");
      end.appendChild(el("div", "v " + sign(r.roi_30d), fmtPct(r.roi_30d, 0) + " 30d"));
      end.appendChild(el("div", "s " + sign(r.roi_7d), fmtPct(r.roi_7d, 0) + " 7d"));
      row.append(main, end);
      row.onclick = () => openMirror(r);
      box.appendChild(row);
    });
  }
  $("#traders-refresh").onclick = () => { toast("Refreshing…"); loadTraders(true); };

  async function openMirror(r) {
    const budget = (state.data && state.data.account.value) || 100;
    const c = $("#sheet-content");
    c.innerHTML = "";
    c.appendChild(el("h3", null, short(r.address)));
    c.appendChild(el("div", "small muted", `${fmtUsd(r.equity, 0)} equity · ${fmtPct(r.roi_30d, 0)} 30d · ${fmtPct(r.roi_7d, 0)} 7d · flags: ${r.flags.join(", ") || "none"}`));
    const form = el("div", "row");
    const inp = el("input");
    inp.type = "number"; inp.value = Math.round(budget); inp.min = 12;
    const go = el("button", "btn btn-dark", "Simulate");
    form.append(inp, go);
    c.appendChild(form);
    const out = el("div");
    c.appendChild(out);
    const copyCmd = el("div", "small muted");
    copyCmd.style.marginTop = "12px";
    copyCmd.innerHTML = `Launch on the VPS:<br><code>hl-agent run config/strategies/copy --copy ${r.address} --name copy-live</code>`;
    c.appendChild(copyCmd);
    const sim = async () => {
      out.innerHTML = "<div class='empty'>Simulating…</div>";
      const m = await api(`/api/mirror/${r.address}?budget=${Number(inp.value) || 100}`);
      out.innerHTML = "";
      for (const [b, plan] of Object.entries(m.plans).sort((a, z) => Number(z[0]) - Number(a[0]))) {
        out.appendChild(el("div", "section-label", `Budget $${b}`)).style.marginTop = "14px";
        const t = el("table", "plan");
        t.innerHTML = "<tr><th>Asset</th><th>Dir</th><th class=r>Alloc</th><th class=r>Moved</th><th class=r>Lev</th><th class=r>Margin</th><th>Verdict</th></tr>";
        for (const ln of plan.lines) {
          const tr = el("tr");
          tr.innerHTML = `<td><b>${ln.asset}</b></td><td><span class="badge badge-${ln.direction.toLowerCase()}">${ln.direction}</span></td><td class=r>${ln.allocation.toFixed(1)}%</td><td class=r>${ln.moved === null ? "n/a" : ln.moved.toFixed(1) + "%"}</td><td class=r>${ln.leverage}x <span class=muted>(${ln.og_leverage}x)</span></td><td class=r>${fmtUsd(ln.margin)}</td><td class="${ln.verdict === "open" ? "pos" : "muted"}">${ln.verdict.replace("skip_", "skip: ")}</td>`;
          t.appendChild(tr);
        }
        out.appendChild(t);
        const s = el("div", "plan-sum");
        s.innerHTML = `<span>opens <b>${plan.opens}/${plan.lines.length}</b></span><span>margin <b>${fmtUsd(plan.margin_committed)}</b></span><span>min budget <b>${fmtUsd(plan.min_budget, 0)}</b></span><span>fresh <b>${plan.fresh_pct === null ? "n/a" : plan.fresh_pct.toFixed(0) + "%"}</b></span><span>scale <b>${plan.scale.toFixed(2)}</b></span>`;
        out.appendChild(s);
      }
    };
    go.onclick = sim;
    openSheet();
    sim();
  }

  function openSheet() { $("#sheet").classList.remove("hidden"); }
  function closeSheet() { $("#sheet").classList.add("hidden"); }
  $("#sheet .sheet-bg").onclick = closeSheet;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

  // ---- activity ----------------------------------------------------------------------
  async function loadActivity() {
    const runs = (state.data && state.data.runs) || [];
    const sel = $("#run-select");
    sel.innerHTML = "";
    for (const r of runs) {
      const o = el("option", null, r.name + (r.kind === "backtest" ? " (backtest)" : ""));
      o.value = r.name;
      sel.appendChild(o);
    }
    const run = runs.find((r) => r.name === state.run) || currentRun() || runs[0];
    if (!run) { $("#activity-run").textContent = "—"; return; }
    sel.value = run.name;
    $("#activity-run").textContent = run.name;
    const [rep, evs] = await Promise.all([api(`/api/runs/${run.name}/report`), api(`/api/runs/${run.name}/events?limit=60`)]);
    const st = $("#stats");
    st.innerHTML = "";
    const m = rep.metrics;
    const cells = m
      ? [["Trades", m.trades], ["Win rate", m.win_rate.toFixed(0) + "%"], ["Net PnL", fmtUsd(m.net_pnl, 0)], ["Max DD", m.drawdown.max_pct.toFixed(1) + "%"]]
      : [["Trades", 0], ["Win rate", "—"], ["Net PnL", "—"], ["Max DD", "—"]];
    for (const [k, v] of cells) {
      const c = el("div", "stat");
      c.append(el("div", "k", k), el("div", "v", String(v)));
      st.appendChild(c);
    }
    const tb = $("#trades");
    tb.innerHTML = "";
    if (!rep.trades.length) tb.appendChild(el("div", "empty", "No closed trade"));
    for (const t of rep.trades.slice(0, 40)) {
      const row = el("div", "row-item");
      const main = el("div", "row-main");
      const title = el("div", "row-title");
      title.append(el("span", null, t.asset), el("span", "badge badge-" + t.direction.toLowerCase(), t.direction));
      main.append(title, el("div", "row-sub", `${t.reason} · ${t.leverage}x · held ${(t.closed_ms - t.opened_ms) / 36e5 < 1 ? Math.round((t.closed_ms - t.opened_ms) / 6e4) + "m" : ((t.closed_ms - t.opened_ms) / 36e5).toFixed(1) + "h"} · ${when(t.closed_ms)}`));
      const end = el("div", "row-end");
      end.append(el("div", "v " + sign(t.pnl_usd), fmtUsd(t.pnl_usd)), el("div", "s " + sign(t.roe_pct), fmtPct(t.roe_pct, 1)));
      row.append(main, end);
      tb.appendChild(row);
    }
    const eb = $("#events");
    eb.innerHTML = "";
    if (!evs.length) eb.appendChild(el("div", "empty", "No event"));
    for (const e of evs) {
      const row = el("div", "row-item");
      const main = el("div", "row-main");
      const title = el("div", "row-title");
      title.append(el("span", null, e.kind), el("span", "badge badge-muted", e.asset || "—"));
      const extra = e.payload && e.payload.pnl_usd !== undefined ? ` · ${fmtUsd(e.payload.pnl_usd)}` : e.payload && e.payload.margin_usd !== undefined ? ` · ${fmtUsd(e.payload.margin_usd)} @ ${e.payload.leverage}x` : "";
      main.append(title, el("div", "row-sub", e.reason + extra));
      row.append(main, el("div", "row-end small muted", when(e.time_ms)));
      eb.appendChild(row);
    }
  }
  $("#run-select").addEventListener("change", (e) => { state.run = e.target.value; loadActivity(); loadEquity(); });

  // ---- profile -----------------------------------------------------------------------
  function renderProfile(d) {
    $("#address").textContent = d.address;
    const box = $("#profile");
    box.innerHTML = "";
    const kv = (k, v, cls) => {
      const row = el("div", "row-item");
      row.append(el("div", "muted small", k), el("div", "v " + (cls || ""), v));
      box.appendChild(row);
    };
    kv("Network", d.network);
    kv("Agent key", d.agent_key.replace(/^agent key: /, ""), /authorised/.test(d.agent_key) ? "pos" : "neg");
    kv("Withdrawable", fmtUsd(d.account.withdrawable));
    kv("Margin used", fmtUsd(d.account.margin_used));
    kv("Runs", String(d.runs.length));
    const theme = el("div", "row-item");
    theme.append(el("div", "muted small", "Theme"));
    const chips = el("div", "chips");
    for (const t of ["auto", "light", "dark"]) {
      const b = el("button", null, t);
      b.classList.toggle("on", (localStorage.getItem("theme") || "auto") === t);
      b.onclick = () => { try { localStorage.setItem("theme", t); } catch (_) {} applyTheme(); renderProfile(d); };
      chips.appendChild(b);
    }
    theme.appendChild(chips);
    box.appendChild(theme);
  }
  $("#login").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify({ token: $("#token").value }) });
      toast("Token saved");
      $("#token").value = "";
      refresh();
      showView("home");
    } catch (err) { toast("Wrong token"); }
  });
  function applyTheme() {
    let t = "auto";
    try { t = localStorage.getItem("theme") || "auto"; } catch (_) {}
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
  }

  // ---- loop --------------------------------------------------------------------------
  async function refresh() {
    try {
      const d = await api("/api/state");
      state.data = d;
      renderHome(d);
      renderProfile(d);
      await loadEquity();
      if (state.view === "activity") await loadActivity();
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message);
    }
  }
  function schedule() {
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

  applyTheme();
  let saved = "home";
  try { saved = localStorage.getItem("view") || "home"; } catch (_) {}
  showView(saved);
  refresh();
  schedule();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
