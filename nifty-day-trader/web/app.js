/* NIFTY Day Trader -- the whole front end. Vanilla JS, no build step, no deps. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let STATE = null;
let CHART = null;      // the loaded chart payload for the trade desk
let TM = null;         // the last Time Machine run
let TICKETS = null;

/* ------------------------------------------------------------------- utils */

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { throw new Error(text.slice(0, 300)); }
  if (data && data.error) throw new Error(data.error);
  return data;
}

function rupees(value, decimals) {
  if (value === null || value === undefined || value === "" || isNaN(value)) return "—";
  const n = Number(value);
  const d = decimals === undefined ? 2 : decimals;
  const fixed = Math.abs(n).toFixed(d);
  let [whole, frac] = fixed.split(".");
  if (whole.length > 3) {
    const tail = whole.slice(-3);
    let head = whole.slice(0, -3);
    const parts = [];
    while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
    if (head) parts.unshift(head);
    whole = parts.join(",") + "," + tail;
  }
  return (n < 0 ? "-₹" : "₹") + whole + (frac ? "." + frac : "");
}

const num = (v, d = 2) => (v === null || v === undefined || isNaN(v) ? "—" : Number(v).toFixed(d));
const pct = (v, d = 2) => (v === null || v === undefined || isNaN(v) ? "—" : (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(d) + "%");
const esc = (s) => String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const cls = (v) => (Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "");

let toastTimer = null;
function toast(message, kind) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast show " + (kind || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast " + (kind || ""); }, 6000);
}

function busy(button, on, label) {
  if (!button) return;
  button.disabled = on;
  if (on) { button.dataset.label = button.textContent; button.textContent = label || "working…"; }
  else if (button.dataset.label) { button.textContent = button.dataset.label; }
}

function readout(items) {
  return '<div class="readout">' + items.map((i) =>
    `<div class="ro"><div class="k">${esc(i.k)}</div><div class="v ${i.c || ""}">${i.v}</div>` +
    (i.n ? `<div class="n">${esc(i.n)}</div>` : "") + "</div>").join("") + "</div>";
}

function table(cols, rows) {
  if (!rows.length) return '<p class="muted small">Nothing here yet.</p>';
  const head = cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + cols.map((c) =>
    `<td class="${c.num ? "num" : ""} ${c.cls ? c.cls(r) : ""}">${c.get(r)}</td>`).join("") + "</tr>").join("");
  return `<div class="tablewrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/* ------------------------------------------------------------- canvas help */

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  // Setting canvas.height rewrites the attribute, so remember the CSS height once.
  if (!canvas.dataset.h) canvas.dataset.h = String(Number(canvas.getAttribute("height")) || 200);
  const height = Number(canvas.dataset.h);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  canvas.style.height = height + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

const COLORS = {
  grid: "#1c2633", text: "#8a9bb0", up: "#26c281", down: "#ef5b5b",
  accent: "#4da3ff", warn: "#f2b134", band: "rgba(77,163,255,.12)", band2: "rgba(77,163,255,.22)",
};

function drawFrame(ctx, w, h, pad, lo, hi, labels, title) {
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillStyle = COLORS.text;
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const y = pad.t + ((h - pad.t - pad.b) * i) / steps;
    const value = hi - ((hi - lo) * i) / steps;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.fillText(value.toFixed(value > 500 ? 0 : 2), pad.l - 6, y);
  }
  if (labels && labels.length) {
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const every = Math.max(1, Math.ceil(labels.length / 8));
    labels.forEach((label, i) => {
      if (i % every) return;
      const x = pad.l + ((w - pad.l - pad.r) * i) / Math.max(1, labels.length - 1);
      ctx.fillText(label, x, h - pad.b + 6);
    });
  }
  if (title) { ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.fillText(title, pad.l, 4); }
}

function drawCandles(canvas, bars, overlays) {
  if (!canvas) return;
  const { ctx, width: w, height: h } = setupCanvas(canvas);
  if (!bars || !bars.length) {
    ctx.fillStyle = COLORS.text; ctx.font = "12px sans-serif"; ctx.textAlign = "center";
    ctx.fillText("Name a symbol and press Load chart to see its 5-minute bars.", w / 2, h / 2);
    return;
  }
  const pad = { l: 56, r: 14, t: 16, b: 22 };
  let lo = Math.min(...bars.map((b) => b.l));
  let hi = Math.max(...bars.map((b) => b.h));
  (overlays || []).forEach((o) => { if (o.value != null) { lo = Math.min(lo, o.value); hi = Math.max(hi, o.value); } });
  const span = (hi - lo) || 1;
  lo -= span * 0.06; hi += span * 0.06;
  const y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  const innerW = w - pad.l - pad.r;
  const step = innerW / bars.length;
  drawFrame(ctx, w, h, pad, lo, hi, bars.map((b) => b.end), null);

  const bodyW = Math.max(1.5, Math.min(9, step * 0.64));
  bars.forEach((bar, i) => {
    const x = pad.l + step * (i + 0.5);
    const rising = bar.c >= bar.o;
    ctx.strokeStyle = rising ? COLORS.up : COLORS.down;
    ctx.fillStyle = rising ? COLORS.up : COLORS.down;
    ctx.beginPath(); ctx.moveTo(x, y(bar.h)); ctx.lineTo(x, y(bar.l)); ctx.stroke();
    const top = y(Math.max(bar.o, bar.c));
    const height = Math.max(1, Math.abs(y(bar.o) - y(bar.c)));
    ctx.fillRect(x - bodyW / 2, top, bodyW, height);
  });

  (overlays || []).forEach((o) => {
    if (o.value == null) return;
    ctx.strokeStyle = o.color; ctx.lineWidth = 1;
    ctx.setLineDash(o.dash || [4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, y(o.value)); ctx.lineTo(w - pad.r, y(o.value)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = o.color; ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left"; ctx.textBaseline = "bottom";
    ctx.fillText(o.label, pad.l + 4, y(o.value) - 2);
  });
}

function drawLine(canvas, points, options) {
  if (!canvas) return;
  const opts = options || {};
  const { ctx, width: w, height: h } = setupCanvas(canvas);
  if (!points || points.length < 2) {
    ctx.fillStyle = COLORS.text; ctx.font = "12px sans-serif"; ctx.textAlign = "center";
    ctx.fillText(opts.empty || "not enough data yet", w / 2, h / 2); return;
  }
  const pad = { l: 60, r: 14, t: 14, b: 22 };
  const values = points.map((p) => p.v);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (opts.base != null) { lo = Math.min(lo, opts.base); hi = Math.max(hi, opts.base); }
  const span = (hi - lo) || Math.abs(hi) * 0.01 || 1;
  lo -= span * 0.08; hi += span * 0.08;
  const y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  const x = (i) => pad.l + ((w - pad.l - pad.r) * i) / (points.length - 1);
  drawFrame(ctx, w, h, pad, lo, hi, points.map((p) => p.t), null);

  if (opts.base != null) {
    ctx.strokeStyle = COLORS.grid; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(pad.l, y(opts.base)); ctx.lineTo(w - pad.r, y(opts.base)); ctx.stroke();
    ctx.setLineDash([]);
  }
  const last = values[values.length - 1];
  const colour = opts.color || (opts.base != null ? (last >= opts.base ? COLORS.up : COLORS.down) : COLORS.accent);
  const grad = ctx.createLinearGradient(0, pad.t, 0, h - pad.b);
  grad.addColorStop(0, colour + "44"); grad.addColorStop(1, colour + "00");
  ctx.beginPath(); ctx.moveTo(x(0), y(values[0]));
  points.forEach((p, i) => ctx.lineTo(x(i), y(p.v)));
  ctx.lineTo(x(points.length - 1), h - pad.b); ctx.lineTo(x(0), h - pad.b); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); ctx.moveTo(x(0), y(values[0]));
  points.forEach((p, i) => ctx.lineTo(x(i), y(p.v)));
  ctx.strokeStyle = colour; ctx.lineWidth = 1.8; ctx.stroke();
}

function drawFan(canvas, bands, plan) {
  if (!canvas) return;
  const { ctx, width: w, height: h } = setupCanvas(canvas);
  if (!bands || !bands.length) return;
  const pad = { l: 56, r: 14, t: 16, b: 22 };
  const all = bands.flatMap((b) => [b.p5, b.p95]).concat([plan.entry, plan.stop, plan.target]);
  let lo = Math.min(...all), hi = Math.max(...all);
  const span = (hi - lo) || 1; lo -= span * 0.05; hi += span * 0.05;
  const y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  const x = (i) => pad.l + ((w - pad.l - pad.r) * i) / Math.max(1, bands.length - 1);
  drawFrame(ctx, w, h, pad, lo, hi, bands.map((b) => b.time), null);

  const area = (key1, key2, colour) => {
    ctx.beginPath();
    bands.forEach((b, i) => (i ? ctx.lineTo(x(i), y(b[key1])) : ctx.moveTo(x(i), y(b[key1]))));
    for (let i = bands.length - 1; i >= 0; i--) ctx.lineTo(x(i), y(bands[i][key2]));
    ctx.closePath(); ctx.fillStyle = colour; ctx.fill();
  };
  area("p5", "p95", COLORS.band);
  area("p25", "p75", COLORS.band2);

  ctx.beginPath();
  bands.forEach((b, i) => (i ? ctx.lineTo(x(i), y(b.p50)) : ctx.moveTo(x(i), y(b.p50))));
  ctx.strokeStyle = COLORS.accent; ctx.lineWidth = 1.6; ctx.stroke();

  [{ v: plan.target, c: COLORS.up, l: "target " + num(plan.target) },
   { v: plan.entry, c: COLORS.text, l: "entry " + num(plan.entry) },
   { v: plan.stop, c: COLORS.down, l: "stop " + num(plan.stop) }].forEach((line) => {
    ctx.strokeStyle = line.c; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.l, y(line.v)); ctx.lineTo(w - pad.r, y(line.v)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = line.c; ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left"; ctx.textBaseline = "bottom";
    ctx.fillText(line.l, pad.l + 4, y(line.v) - 2);
  });
}

function drawHistogram(canvas, bins, plan) {
  if (!canvas) return;
  const { ctx, width: w, height: h } = setupCanvas(canvas);
  if (!bins || !bins.length) return;
  const pad = { l: 40, r: 12, t: 12, b: 24 };
  const max = Math.max(...bins.map((b) => b.count)) || 1;
  const step = (w - pad.l - pad.r) / bins.length;
  ctx.font = "10px ui-monospace, monospace"; ctx.fillStyle = COLORS.text;
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  bins.forEach((bin, i) => {
    const barH = ((h - pad.t - pad.b) * bin.count) / max;
    const x = pad.l + step * i;
    const mid = (bin.from + bin.to) / 2;
    ctx.fillStyle = mid >= plan.target ? COLORS.up : mid <= plan.stop ? COLORS.down : COLORS.accent + "99";
    ctx.fillRect(x + 1, h - pad.b - barH, step - 2, barH);
  });
  ctx.fillStyle = COLORS.text;
  [0, Math.floor(bins.length / 2), bins.length - 1].forEach((i) => {
    const x = pad.l + step * (i + 0.5);
    ctx.fillText(num(bins[i].from, 0), x, h - pad.b + 5);
  });
}

/* ------------------------------------------------------------------- state */

async function refreshState() {
  try {
    STATE = await api("/api/state");
  } catch (err) { toast("Could not reach the app: " + err.message, "bad"); return; }
  renderTop(); renderKpis(); renderChain(); renderEvents();
  drawLine($("#equityChart"),
    (STATE.account.equity_curve || []).map((p) => ({ t: p.t.slice(11), v: p.equity })),
    { base: STATE.account.opening_balance, empty: "the equity curve starts with your first fill" });
  $("#equityNote").textContent = "opening " + rupees(STATE.account.opening_balance) +
    " · now " + rupees(STATE.account.equity);
  renderAccountBox();
}

function renderTop() {
  const market = STATE.market, account = STATE.account;
  const disc = STATE.discipline || {};
  const dotClass = market.state === "open" ? "open" : market.state === "pre-open" ? "pre" : "closed";
  const reg = disc.regime;
  const cb = disc.circuit_breaker;
  const gateText = !reg ? '<span class="muted">unread</span>'
    : `<span class="${reg.allows_new_entries ? "up" : "down"}">${esc(reg.regime)}</span>`;
  const cbText = cb && cb.recommendation !== "TRADING_ALLOWED"
    ? `<span class="down">${esc(cb.recommendation)}</span>` : "";
  $("#topstats").innerHTML = [
    { k: "Regime", v: gateText + (cbText ? " " + cbText : ""),
      n: reg ? reg.recommendation : "read it in Regime & edge" },
    { k: "NSE", v: `<span class="dot ${dotClass}"></span>${market.state}`, n: market.now_ist.slice(11, 16) + " IST" },
    { k: "Equity", v: rupees(account.equity) },
    { k: "Cash", v: rupees(account.cash) },
    { k: "Day P&L", v: `<span class="${cls(account.realised_today + account.unrealised)}">${rupees(account.realised_today + account.unrealised)}</span>` },
    { k: "Open", v: account.open_positions },
  ].map((s) => `<div class="topstat"><div class="k">${s.k}</div><div class="v">${s.v}</div></div>`).join("");
}

function renderKpis() {
  const a = STATE.account;
  $("#kpis").innerHTML = [
    { k: "Paper account", v: `<span style="font-size:14px">${esc(a.account)}</span>`, n: "opened " + esc(a.created_at || "—") },
    { k: "Equity", v: rupees(a.equity), n: `cash ${rupees(a.cash)} + holdings ${rupees(a.holdings_value)}`, c: cls(a.total_return) },
    { k: "Total return", v: rupees(a.total_return) + ` <span class="small">(${pct(a.total_return_pct)})</span>`, n: `realised ${rupees(a.realised_pnl)} · unrealised ${rupees(a.unrealised)}`, c: cls(a.total_return) },
    { k: "Charges paid", v: rupees(a.charges_paid), n: "estimated NSE intraday charges" },
  ].map((s) => `<div class="kpi"><div class="k">${s.k}</div><div class="v ${s.c || ""}">${s.v}</div><div class="n">${s.n || ""}</div></div>`).join("");
}

function renderChain() {
  const c = STATE.chain;
  const rows = [
    ["1 Scanner", c.watchlist.mode ? `${c.watchlist.hits} hit(s) from the ${esc(c.watchlist.mode)} scan at ${esc(c.watchlist.as_of || "")}` : "not run yet", !!c.watchlist.mode],
    ["2 Entry", c.entry ? `${esc(c.entry.symbol)} — ${esc(c.entry.status)}${c.entry.entry ? " at " + num(c.entry.entry) + " (" + esc(c.entry.entry_time) + ")" : ""}` : "no entry saved", !!(c.entry && c.entry.status === "triggered")],
    ["3 Exit plan", c.plan ? `${esc(c.plan.symbol)} entry ${num(c.plan.entry)} · stop ${num(c.plan.stop)} · target ${num(c.plan.target)} · R:R ${num(c.plan.reward_risk)}` : "no plan", !!c.plan],
    ["5 Order", c.orders ? `${c.orders.qty} shares, ${rupees(c.orders.value)}, max loss ${rupees(c.orders.max_loss)}` : "nothing sized", !!c.orders],
    ["6 Risk", c.risk ? `${c.risk.passed} passed, ${c.risk.blocked} blocked (${esc(c.risk.date)})` : "not checked", !!(c.risk && c.risk.passed)],
  ];
  $("#chainState").innerHTML = rows.map(([name, text, on]) =>
    `<div class="chain-row ${on ? "on" : ""}"><span class="nm">${esc(name)}</span><span>${text}</span></div>`).join("");
}

function renderEvents() {
  const events = STATE.events || [];
  $("#events").innerHTML = events.length
    ? events.map((e) => `<div class="ev"><span class="t">${esc(e.at.slice(11, 16))}</span><span class="kind">${esc(e.kind)}</span><span>${esc(e.text)}</span></div>`).join("")
    : '<p class="muted small">Nothing has happened yet. Run a scan.</p>';
}

async function loadIndexChart() {
  try {
    const data = await api("/api/chart?symbol=%5ENSEI&range=1d&interval=5m");
    drawLine($("#indexChart"), data.bars.map((b) => ({ t: b.end, v: b.c })),
      { base: data.meta.previous_close, empty: "no NIFTY bars yet today" });
    const last = data.bars.length ? data.bars[data.bars.length - 1].c : data.meta.price;
    const prev = data.meta.previous_close;
    $("#indexNote").innerHTML = `<span class="mono">${num(last)}</span> ` +
      (prev ? `<span class="${cls(last - prev)}">${pct(((last - prev) / prev) * 100)}</span>` : "") +
      ` · ${data.bars.length} bars · delayed`;
  } catch (err) { $("#indexNote").textContent = "NIFTY chart unavailable: " + err.message; }
}

/* ----------------------------------------------------------------- scanner */

async function runScan(mode, button) {
  const status = $("#scanStatus");
  status.className = "status";
  status.textContent = mode === "orb"
    ? "Stage 1: 5-minute closes for every name. Stage 2: real highs and lows for each candidate. This takes a few seconds…"
    : "Fetching three months of daily closes…";
  busy(button, true, "scanning…");
  try {
    const out = await api("/api/scan", {
      mode, group: $("#scanGroup").value,
      or_minutes: Number($("#scanOr").value),
      min_price: Number($("#scanMinPrice").value || 0),
    });
    renderScan(out);
    status.className = "status ok";
    status.textContent = out.error || summariseScan(out);
    refreshState();
  } catch (err) {
    status.className = "status err"; status.textContent = err.message;
  } finally { busy(button, false); }
}

function summariseScan(out) {
  if (out.mode === "orb") {
    return `${out.usable} of ${out.symbols} charts usable · ${out.candidates} candidates · ` +
      `${out.confirmed} confirmed with real highs and lows · ${out.failed} failed or had no data · ` +
      `${(out.hits || []).length} closed above their opening-range high by ${out.as_of.slice(11)}`;
  }
  return `${out.fetched} of ${out.symbols} series fetched · ${out.scanned} scanned · ` +
    `${(out.hits || []).length} matched a rule · as of ${out.as_of}`;
}

function renderScan(out) {
  const hits = out.hits || [];
  if (!hits.length) {
    $("#scanResult").innerHTML = `<div class="note">${esc(out.error || "No chart matched the rule.")}</div>`;
    return;
  }
  const pick = (s) => `<a class="linkish" onclick="pickSymbol('${s}')">trade ▸</a>`;
  const cols = out.mode === "orb" ? [
    { label: "Trigger", get: (r) => `<span class="mono">${esc(r.trigger_time)}</span>` },
    { label: "Symbol", get: (r) => `<strong>${esc(r.display)}</strong><div class="muted small">${esc(r.name)}</div>` },
    { label: "Close", num: true, get: (r) => num(r.price) },
    { label: "Range", num: true, get: (r) => `${num(r.or_low)} – ${num(r.or_high)}` },
    { label: "Over", num: true, get: (r) => `<span class="up">${pct(r.over_pct)}</span>` },
    { label: "Vol ×", num: true, get: (r) => (r.volume_x ? num(r.volume_x) + "×" : "—") },
    { label: "Last", num: true, get: (r) => num(r.last) },
    { label: "", get: (r) => pick(r.display) },
  ] : [
    { label: "Symbol", get: (r) => `<strong>${esc(r.display)}</strong><div class="muted small">${esc(r.name)}</div>` },
    { label: "Setup", get: (r) => `<span class="pill">${esc(r.setup)}</span>` },
    { label: "Close", num: true, get: (r) => num(r.price) },
    { label: "Change", num: true, cls: (r) => cls(r.change_pct), get: (r) => pct(r.change_pct) },
    { label: "RSI 14", num: true, get: (r) => num(r.rsi14, 1) },
    { label: "MA 20", num: true, get: (r) => num(r.ma20) },
    { label: "Why it matched", get: (r) => `<span class="muted small">${esc(r.why)}</span>` },
    { label: "", get: (r) => pick(r.display) },
  ];
  $("#scanResult").innerHTML = table(cols, hits) +
    `<div class="note">${esc(out.note || "")}</div>`;
}

/* ------------------------------------------------------------------ invest */

function renderLongtest(r) {
  if (!r || !r.arms) { $("#ltOut").innerHTML = '<p class="muted small">Nothing run yet.</p>'; return; }
  const intr = r.intraday_for_contrast;
  $("#ltOut").innerHTML = table([
    { label: "Arm", get: (a) => `<strong>${esc(a.label)}</strong>` },
    { label: "Return", num: true, cls: (a) => cls(a.total_return_pct), get: (a) => pct(a.total_return_pct) },
    { label: "A year", num: true, cls: (a) => cls(a.cagr_pct), get: (a) => pct(a.cagr_pct) },
    { label: "Worst drawdown", num: true, get: (a) => num(a.max_drawdown_pct) + " %" },
    { label: "Trades", num: true, get: (a) => a.trades },
    { label: "Charges", num: true, get: (a) => rupees(a.charges) },
    { label: "Charges / account", num: true, get: (a) => num(a.charges_pct_of_opening) + " %" },
    { label: "Turnover", num: true, get: (a) => num(a.turnover_x) + "×" },
  ], r.arms) +
    (intr ? `<div class="note bad">For contrast, the intraday arm over 60 sessions: ${intr.trades} trades, ${rupees(intr.net)} net, ${rupees(intr.charges)} of charges, ${num(intr.expectancy_r, 4)}R a trade.</div>` : "") +
    `<div class="note"><strong>${esc(r.verdict)}</strong></div>` +
    `<div class="note">${r.sessions} sessions, ${esc(r.from)} to ${esc(r.to)}, ${r.symbols} names. Halves split at ${esc(r.split_at)}.</div>` +
    (r.limits || []).map((l) => `<div class="note warn">${esc(l)}</div>`).join("");
}

async function runLongtest(kind, button) {
  const status = $("#ltStatus");
  status.className = "status";
  status.textContent = kind === "load" ? "Loading…" : "Pulling two years of daily bars for the universe. About 20 seconds…";
  busy(button, true, "running…");
  try {
    const r = await api("/api/longtest", kind === "load" ? { saved: true } : {});
    renderLongtest(r);
    status.className = "status ok";
    status.textContent = r && r.arms ? `${r.sessions} sessions, ${r.symbols} names · ${r.as_of}` : "Nothing saved yet.";
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy(button, false); }
}

function renderTargets(t) {
  if (!t || !t.targets) return;
  $("#tgOut").innerHTML = (t.targets.length ? table([
    { label: "Symbol", get: (r) => `<strong>${esc(r.display)}</strong><div class="muted small">${esc(r.name)}</div>` },
    { label: "Weight", num: true, get: (r) => num(r.weight) + " %" + (r.capped ? ' <span class="pill wait">capped</span>' : "") },
    { label: "Score", num: true, get: (r) => r.score === undefined ? "—" : pct(r.score, 1) },
    { label: "Last", num: true, get: (r) => num(r.price) },
    { label: "Why it is in", get: (r) => `<span class="muted small">${esc(r.why)}</span>` },
  ], t.targets) : '<p class="muted small">Nothing passed the screen.</p>') +
    `<div class="note">${esc(t.method_note || "")} · ${t.targets.length} names, ${num(t.total_weight)}% of the stock sleeve · ${t.rejected.length} rejected.</div>` +
    (t.rejected || []).slice(0, 6).map((r) => `<div class="small muted" style="padding:2px 0">✗ <strong>${esc(r.display)}</strong> — ${esc(r.why)}</div>`).join("") +
    `<div class="note">${esc(t.note || "")}</div>`;
}

async function buildTargets(saved, button) {
  const status = $("#tgStatus");
  status.className = "status";
  status.textContent = saved ? "Loading…" : "Pulling daily history and ranking…";
  busy(button, true, "working…");
  try {
    const t = await api("/api/targets", saved ? { saved: true } : {
      method: $("#tgMethod").value, hold: Number($("#tgHold").value),
    });
    renderTargets(t);
    status.className = "status ok";
    status.textContent = t.targets ? `${t.targets.length} names · ${t.as_of || ""}` : "";
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy(button, false); }
}

async function showDrift(write, button) {
  const status = $("#rbStatus");
  status.className = "status";
  status.textContent = "Valuing every holding and target at the latest prices…";
  busy(button, true, "working…");
  try {
    const d = await api("/api/drift", { band: Number($("#rbBand").value), write: !!write });
    if (d.error) { status.className = "status err"; status.textContent = d.error; return; }
    $("#rbOut").innerHTML = table([
      { label: "Symbol", get: (r) => `<strong>${esc(r.display)}</strong>` },
      { label: "Held", num: true, get: (r) => r.qty },
      { label: "Value", num: true, get: (r) => rupees(r.value) },
      { label: "Now", num: true, get: (r) => num(r.current_pct) + " %" },
      { label: "Target", num: true, get: (r) => num(r.target_pct) + " %" },
      { label: "Drift", num: true, cls: (r) => cls(r.drift_pts), get: (r) => (r.drift_pts >= 0 ? "+" : "") + num(r.drift_pts) + " pts" },
      { label: "", get: (r) => r.outside_band ? '<span class="pill wait">outside the band</span>' : (r.in_targets ? "" : '<span class="pill">not a target</span>') },
    ], d.rows.slice(0, 20)) +
      (d.orders.length ? "<h3 style=\"margin:14px 0 6px\">Proposed trades</h3>" + table([
        { label: "Side", get: (o) => `<span class="pill ${o.side === "sell" ? "wait" : ""}">${esc(o.side)}</span>` },
        { label: "Symbol", get: (o) => esc(o.display) },
        { label: "Qty", num: true, get: (o) => o.qty },
        { label: "Value", num: true, get: (o) => rupees(o.value) },
        { label: "Why", get: (o) => `<span class="muted small">${esc(o.why)}</span>` },
      ], d.orders) : "") +
      `<div class="note">${esc(d.message)} Account ${rupees(d.account)}, cash ${rupees(d.cash)}, buffer ${rupees(d.cash_buffer)}.</div>` +
      (d.untracked.length ? `<div class="note warn">Held but not in the targets: ${esc(d.untracked.join(", "))}. Left alone.</div>` : "") +
      `<div class="note">${esc(d.note)}</div>`;
    status.className = "status ok";
    status.textContent = write
      ? `${d.orders.length} proposal(s) written — run the risk check on the trade desk, then say yes to each.`
      : `${d.outside_band} outside the band.`;
    if (write) refreshState();
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy(button, false); }
}

async function approveBasket(button) {
  const status = $("#rbStatus");
  status.className = "status";
  status.textContent = "Running the risk check over the basket…";
  busy(button, true, "checking…");
  try {
    const checked = await api("/api/risk", {});
    const tickets = await api("/api/tickets");
    const ready = (tickets.tickets || []).filter((t) => t.state === "READY");
    const blocked = (tickets.tickets || []).filter((t) => t.state === "BLOCKED");
    const value = ready.reduce((sum, t) => sum + Number(t.order.value || 0), 0);
    $("#basketOut").innerHTML =
      `<div class="note ${ready.length ? "good" : "bad"}">${checked.passed} passed, ${checked.blocked} blocked. ` +
      `${ready.length} order(s) are READY, ${rupees(value)} in all.</div>` +
      (blocked.length ? `<div class="note bad">Blocked: ${esc(blocked.slice(0, 3).map((t) => t.why).join(" · "))}</div>` : "");
    status.className = ready.length ? "status ok" : "status err";
    status.textContent = ready.length ? `${ready.length} ready.` : "Nothing is ready to approve.";
    if (!ready.length) return;

    $("#modalTitle").textContent = "One yes for the whole basket";
    $("#modalBody").innerHTML =
      `<div class="ticket">
         <div class="hd">${ready.length} order(s), ${esc(rupees(value))}, PAPER account, delivery segment</div>
         ${ready.slice(0, 8).map((t) => `<div>${esc(t.order.side.toUpperCase())} ${t.order.qty} ${esc(t.order.display || t.order.symbol)} — ${esc(rupees(t.order.value))}</div>`).join("")}
         ${ready.length > 8 ? `<div class="muted">… and ${ready.length - 8} more</div>` : ""}
       </div>
       <p class="muted small" style="margin-top:10px">The skills insist on one yes per order and never
       “yes to all”. That rule is right where a yes can reach a real broker; it cannot here, and this
       approval is recorded as a <strong>basket</strong> naming every order it covers, so the log never
       pretends you said yes ${ready.length} times. Change any order and its fingerprint changes, and
       this approval stops covering it.</p>
       <input id="yesInput" placeholder="type yes" autocomplete="off" />`;
    $("#modal").classList.remove("hidden");
    setTimeout(() => $("#yesInput") && $("#yesInput").focus(), 50);
    $("#modalOk").textContent = "Record my yes for the basket";
    $("#modalOk").onclick = async () => {
      try {
        const out = await api("/api/approve-basket", { reply: ($("#yesInput").value || "").trim() });
        if (!out.ok) { toast(out.message, "bad"); return; }
        $("#modal").classList.add("hidden");
        toast(out.message, "good");
        const filled = await api("/api/place-basket", {});
        $("#basketOut").innerHTML =
          `<div class="note ${filled.filled ? "good" : "bad"}"><strong>${esc(filled.message)}</strong></div>` +
          (filled.results || []).filter((r) => !r.ok).slice(0, 6).map((r) => `<div class="note bad">${esc(r.display)}: ${esc(r.message)}</div>`).join("") +
          `<div class="note">Cash ${rupees(filled.cash)} · equity ${rupees(filled.equity)} · ${filled.open_positions} open position(s).</div>`;
        toast(filled.message, filled.filled ? "good" : "bad");
        refreshState(); loadPositions(); loadTaxLots(false, $("#txBtn"));
      } catch (err) { toast(err.message, "bad"); }
    };
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy(button, false); }
}

async function loadTaxLots(harvest, button) {
  busy(button, true);
  try {
    const t = await api("/api/taxlots", harvest ? { harvest: true, min_loss: 500 } : {});
    if (harvest) {
      $("#txOut").innerHTML = (t.candidates.length ? table([
        { label: "Lot", get: (r) => `<span class="mono small">${esc(r.id)}</span>` },
        { label: "Symbol", get: (r) => esc(r.display) },
        { label: "Bought", get: (r) => `<span class="mono small">${esc(String(r.bought).slice(0, 10))}</span>` },
        { label: "Qty", num: true, get: (r) => r.qty },
        { label: "Loss", num: true, cls: () => "down", get: (r) => rupees(r.loss) },
        { label: "Term", get: (r) => `<span class="pill">${esc(r.term)}</span>` },
      ], t.candidates) : "") +
        `<div class="note">${esc(t.message)}</div><div class="note warn">${esc(t.note)}</div>`;
      return;
    }
    $("#txOut").innerHTML = readout([
      { k: "Open lots", v: t.lot_count },
      { k: "Cost basis", v: rupees(t.totals.cost) },
      { k: "Value now", v: rupees(t.totals.value) },
      { k: "Unrealised", v: rupees(t.totals.unrealised), c: cls(t.totals.unrealised) },
      { k: "Realised, short term", v: rupees(t.realised.short_term), c: cls(t.realised.short_term), n: "held under 12 months" },
      { k: "Realised, long term", v: rupees(t.realised.long_term), c: cls(t.realised.long_term), n: "held 12 months or more" },
    ]) +
      (t.lots.length ? table([
        { label: "Lot", get: (r) => `<span class="mono small">${esc(r.id)}</span>` },
        { label: "Symbol", get: (r) => esc(r.display) },
        { label: "Bought", get: (r) => `<span class="mono small">${esc(String(r.bought).slice(0, 16))}</span>` },
        { label: "Qty", num: true, get: (r) => r.qty },
        { label: "Paid", num: true, get: (r) => num(r.price_paid) },
        { label: "Last", num: true, get: (r) => num(r.last) },
        { label: "Unrealised", num: true, cls: (r) => cls(r.unrealised), get: (r) => rupees(r.unrealised) + ` <span class="small">(${pct(r.unrealised_pct)})</span>` },
        { label: "Term", get: (r) => `<span class="pill ${r.term === "long" ? "pass" : ""}">${esc(r.term)}</span>` + (r.days_to_long_term > 0 ? `<div class="muted small">long on ${esc(r.long_term_on)}</div>` : "") },
      ], t.lots) : '<p class="muted small">No open lots. Lots are rebuilt from the paper account\'s own fills.</p>') +
      `<div class="note warn">${esc(t.note)}</div>`;
  } catch (err) { $("#txOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy(button, false); }
}

/* ---------------------------------------------------------------- the feed */

let FEED_FILTER = { symbol: null, tag: null };

function renderFeed(f) {
  if (!f) return;
  $("#feedMeta").textContent = `${f.total} headlines · ${f.polls} polls · ${f.unread} unread · ${f.as_of || "not polled yet"}`;
  const badge = $("#feedBadge");
  badge.textContent = f.unread || "";
  badge.classList.toggle("hidden", !f.unread);
  $("#feedWatching").innerHTML = (f.watching || []).length
    ? `<span class="muted">following:</span> ${(f.watching || []).map((w) => `<span class="pill">${esc(w)}</span>`).join(" ")}` +
      (f.last_polled && f.last_polled.length ? ` <span class="muted">· last cycle fetched ${esc(f.last_polled.join(", "))}</span>` : "")
    : '<span class="muted">following the market only — hold, plan or scan something and it follows that too.</span>';
  const items = f.items || [];
  $("#feedOut").innerHTML = items.length ? items.map((i) => `
    <div class="feeditem ${i.unread ? "unread" : ""}">
      <div class="when">${esc(i.published ? i.published.slice(5) : "—")}</div>
      <div class="body">
        <div class="head"><a class="linkish" href="${esc(i.link)}" target="_blank" rel="noopener">${esc(i.title)}</a></div>
        <div class="meta">
          <span>${esc(i.source)}</span>
          ${(i.symbols || []).map((s) => `<span class="pill">${esc(s.replace(".NS", ""))}</span>`).join("")}
          ${(i.tags || []).map((t) => `<span class="pill ${["results", "regulatory"].includes(t) ? "wait" : ""}">${esc(t)}</span>`).join("")}
        </div>
      </div>
    </div>`).join("")
    : '<p class="muted small">Nothing in the feed yet. Press “Poll now”, or leave the app running and it fills itself.</p>';
  (f.errors || []).forEach((e) => { $("#feedOut").innerHTML += `<div class="note bad">${esc(e)}</div>`; });
  $("#feedOut").innerHTML += `<div class="note">${esc(f.note || "")}</div>`;
}

async function loadFeed() {
  try {
    const qs = new URLSearchParams();
    if (FEED_FILTER.symbol) qs.set("symbol", FEED_FILTER.symbol);
    if (FEED_FILTER.tag) qs.set("tag", FEED_FILTER.tag);
    renderFeed(await api("/api/feed" + (qs.toString() ? "?" + qs : "")));
  } catch (err) { /* the feed is best effort */ }
}

async function pollFeed() {
  const status = $("#feedStatus");
  status.className = "status";
  status.textContent = "Polling the market feed and the next names on the cursor…";
  busy($("#feedPollBtn"), true, "polling…");
  try {
    renderFeed(await api("/api/feed/poll", {}));
    status.className = "status ok";
    status.textContent = "Polled.";
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy($("#feedPollBtn"), false); }
}

async function updateFeedBadge() {
  try {
    const f = await api("/api/feed?limit=1");
    const badge = $("#feedBadge");
    badge.textContent = f.unread || "";
    badge.classList.toggle("hidden", !f.unread);
  } catch (err) { /* ignore */ }
}

/* ------------------------------------------------------------------- watch */

function renderWatch(w) {
  if (!w) return;
  const g = w.gates || {};
  $("#watchGates").innerHTML = g.clear
    ? `<span class="pill pass">gates clear</span> ${g.trades_left} trade(s) left today`
    : `<span class="pill block">entries blocked</span> ${esc((g.blocking || [])[0] || "")}`;
  const rows = w.candidates || [];
  $("#watchOut").innerHTML = (rows.length ? table([
    { label: "Symbol", get: (r) => `<strong>${esc(r.display)}</strong><div class="muted small">${esc(r.name)}</div>` },
    { label: "Reclaimed", get: (r) => `<span class="mono">${esc(r.entry_time)}</span><div class="muted small">lost it ${esc(r.broke_at || "—")}</div>` },
    { label: "Entry", num: true, get: (r) => num(r.entry) },
    { label: "Stop", num: true, get: (r) => `<span class="down">${num(r.stop)}</span>` },
    { label: "Target", num: true, get: (r) => `<span class="up">${num(r.target)}</span>` },
    { label: "Risk/share", num: true, get: (r) => num(r.risk_per_share) },
    { label: "Shares", num: true, get: (r) => r.qty },
    { label: "Max loss", num: true, get: (r) => rupees(r.max_loss) },
    { label: "Last", num: true, cls: (r) => cls(r.drift_pct), get: (r) => num(r.last) + `<div class="muted small">${pct(r.drift_pct)}</div>` },
    { label: "State", get: (r) => r.stale ? '<span class="pill wait">stale</span>' : r.still_valid ? '<span class="pill pass">fresh</span>' : '<span class="pill block">past its levels</span>' },
    { label: "", get: (r) => `<a class="linkish" onclick="takeCandidate('${r.display}')">trade &#9656;</a>` },
  ], rows) : "") +
    `<div class="note ${g.clear && w.fresh ? "good" : ""}">${esc(w.message || "")}</div>` +
    (!g.clear && (g.blocking || []).length ? (g.blocking).map((b) => `<div class="note bad">${esc(b)}</div>`).join("") : "") +
    `<div class="note">${esc(w.note || "")}</div>`;
}

async function runWatch(force) {
  const status = $("#watchStatus");
  status.className = "status";
  status.textContent = "Scanning the universe for reclaims…";
  busy($("#watchBtn"), true, "scanning…");
  try {
    const w = await api("/api/watch", { force: !!force });
    renderWatch(w);
    status.className = "status ok";
    status.textContent = `${w.scanned} names scanned, ${w.checked} checked with real highs and lows · ${w.as_of}`;
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy($("#watchBtn"), false); }
}

async function loadWatchSaved() {
  try { renderWatch(await api("/api/watch/saved")); } catch (err) { /* nothing yet */ }
}

function takeCandidate(display) {
  $("#tradeSymbol").value = display;
  $("#tradeSetup").value = "or_low_reclaim";
  $("#stopRule").value = "swing-low";
  showTab("trade");
  loadSymbol();
  runEntry();
}
window.takeCandidate = takeCandidate;

/* -------------------------------------------------------------- discipline */

function regimePill(r) {
  if (!r) return '<span class="pill wait">not read yet</span>';
  const cls = r.allows_new_entries ? "pass" : "block";
  return `<span class="pill ${cls}">${esc(r.regime)} &middot; ${esc(r.recommendation)}</span>`;
}

async function runRegime(refresh) {
  busy($("#regimeBtn"), true, "reading…");
  try {
    const r = await api("/api/regime", { refresh: !!refresh });
    const d = r.distribution || {};
    const p = r.participation || {};
    $("#regimeOut").innerHTML =
      readout([
        { k: "Regime", v: esc(r.regime), c: r.allows_new_entries ? "up" : "down", n: r.bias },
        { k: "Recommendation", v: `<span style="font-size:13px">${esc(r.recommendation)}</span>`,
          c: r.allows_new_entries ? "up" : "down",
          n: r.allows_new_entries ? "new long entries allowed" : "no new long entries" },
        { k: "Market risk", v: esc(r.risk_level), c: r.risk_level === "NORMAL" ? "up" : r.risk_level === "SEVERE" ? "down" : "warnc",
          n: `distribution days: ${d.d5} in 5, ${d.d15} in 15, ${d.d25} in 25` },
        { k: "Exposure ceiling", v: num(r.exposure_ceiling_pct, 0) + " %" },
        { k: "NIFTY", v: num(r.last_close), n: `21 EMA ${num(r.ema21)} · 50 SMA ${num(r.sma50)} · ${r.above_ema21 && r.above_sma50 ? "above both" : "below one"}` },
        { k: "Participation", v: num(p.above_ma50_pct, 0) + " %", n: `above the 50-day · ${num(p.above_ma20_pct, 0)}% above the 20-day, of ${p.counted} names` },
      ]) +
      `<div class="note ${r.allows_new_entries ? "good" : "bad"}">${esc(r.rationale)}</div>` +
      (r.adjustments || []).map((a) => `<div class="note warn">${esc(a)}</div>`).join("") +
      (d.active && d.active.length ? table([
        { label: "Distribution day", get: (x) => `<span class="mono">${esc(x.date)}</span>` },
        { label: "Fell", num: true, get: (x) => "−" + num(x.drop_pct) + " %" },
        { label: "Sessions ago", num: true, get: (x) => x.age_sessions },
      ], d.active) : '<p class="muted small">No active distribution day: no session in the last 25 closed 0.2% or more lower on higher volume without being cancelled by a 5% gain.</p>') +
      `<div class="note">${esc(r.source)}</div><div class="note">${esc(r.note)}</div>`;
    refreshState();
  } catch (err) { $("#regimeOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#regimeBtn"), false); }
}

async function loadDiscipline() {
  busy($("#disciplineBtn"), true);
  try {
    const d = await api("/api/discipline");
    const cb = d.circuit_breaker, h = d.heat, g = d.gate;
    const cbClass = cb.recommendation === "TRADING_ALLOWED" ? "up" : "down";
    $("#disciplineOut").innerHTML =
      readout([
        { k: "Circuit breaker", v: `<span style="font-size:13px">${esc(cb.recommendation)}</span>`, c: cbClass },
        { k: "Realised today", v: rupees(cb.metrics.realised_today), c: cls(cb.metrics.realised_today), n: `limit ${rupees(cb.limits.daily)}` },
        { k: "This week", v: rupees(cb.metrics.realised_week), c: cls(cb.metrics.realised_week), n: `limit ${rupees(cb.limits.weekly)}` },
        { k: "Losing streak", v: cb.metrics.consecutive_losses, n: `cooldown after ${d.settings.losing_streak_n}` },
        { k: "Portfolio heat", v: num(h.heat_pct) + " %", c: h.over ? "down" : "", n: `${rupees(h.heat)} open risk of a ${rupees(h.limit)} cap` },
        { k: "Pre-trade gate", v: `<span class="pill ${g.decision === "GO" ? "pass" : g.decision === "NO_GO" ? "block" : "wait"}">${esc(g.decision)}</span>` },
      ]) +
      (cb.triggered_rules || []).map((t) => `<div class="note bad"><strong>${esc(t.rule)}</strong> — ${esc(t.detail)} Active until ${esc(t.active_until)}.</div>`).join("") +
      `<div class="note">${esc(cb.rationale)}</div>` +
      (g.checks || []).map((c) => `<div class="small" style="padding:3px 0"><span class="${c.ok ? "up" : "down"}">${c.ok ? "✓" : "✗"}</span> <strong>${esc(c.check)}</strong> <span class="muted">${esc(c.detail)}</span></div>`).join("") +
      `<div class="note">${esc(cb.note)}</div>`;
  } catch (err) { $("#disciplineOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#disciplineBtn"), false); }
}

async function loadPerformance() {
  busy($("#perfBtn"), true);
  try {
    const p = await api("/api/performance");
    $("#perfOut").innerHTML =
      readout([
        { k: "Round trips", v: p.round_trips, n: `${p.wins} win · ${p.losses} loss` },
        { k: "Win rate", v: p.win_rate_pct === null ? "—" : num(p.win_rate_pct, 1) + " %" },
        { k: "Avg win / loss", v: `${rupees(p.avg_win)} / ${rupees(p.avg_loss)}`, n: p.payoff_ratio ? "payoff " + num(p.payoff_ratio) : "" },
        { k: "Expectancy", v: rupees(p.expectancy), c: cls(p.expectancy), n: p.expectancy_r === null ? "" : num(p.expectancy_r, 3) + "R a trade" },
        { k: "Profit factor", v: p.profit_factor === null ? "—" : num(p.profit_factor) },
        { k: "Charges", v: rupees(p.charges), n: p.charges_share_pct === null ? "" : num(p.charges_share_pct, 0) + "% of gross profit" },
        { k: "Max drawdown", v: rupees(p.drawdown.max_drawdown), c: "down", n: num(p.drawdown.max_drawdown_pct) + "%" },
        { k: "Half Kelly", v: p.half_kelly_pct === null ? "—" : num(p.half_kelly_pct, 1) + " %", n: p.kelly_ready ? "used as a sizing ceiling" : "needs 20+ closed trades" },
      ]) +
      `<div class="note ${p.expectancy > 0 ? "good" : p.expectancy < 0 ? "bad" : ""}">${esc(p.verdict)}</div>` +
      (p.caveats || []).map((c) => `<div class="note warn">${esc(c)}</div>`).join("") +
      (p.behaviour || []).map((b) => `<div class="note warn"><strong>${esc(b.tag)}</strong> — ${esc(b.detail)}</div>`).join("") +
      `<div class="note">${esc(p.note)}</div>`;
  } catch (err) { $("#perfOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#perfBtn"), false); }
}

function renderBacktest(bt, sweep) {
  let html = "";
  if (bt && bt.ungated) {
    const arm = (a) => a && a.trades ? readout([
      { k: "Trades", v: a.trades, n: `over ${a.sessions_traded} sessions` },
      { k: "Win rate", v: num(a.win_rate_pct, 1) + " %", n: `${a.wins} win · ${a.losses} loss` },
      { k: "Expectancy", v: (a.expectancy_r >= 0 ? "+" : "") + num(a.expectancy_r, 4) + "R", c: cls(a.expectancy_r), n: "a trade" },
      { k: "Total", v: (a.total_r >= 0 ? "+" : "") + num(a.total_r) + "R", c: cls(a.total_r) },
      { k: "Worst drawdown", v: num(a.max_drawdown_r) + "R", c: "down" },
      { k: "Net", v: rupees(a.net), c: cls(a.net), n: `after ${rupees(a.charges)} of charges` },
    ]) : '<p class="muted small">no trades</p>';
    html += `<h3 style="margin:10px 0 4px">Every signal</h3>${arm(bt.ungated)}
             <h3 style="margin:14px 0 4px">Only when the regime gate allowed new entries</h3>${arm(bt.gated)}
             <div class="note"><strong>${esc(bt.verdict)}</strong></div>
             <div class="note">Setup: ${esc(bt.setup.rule)}, stop at ${esc(bt.setup.stop)}, target ${esc(bt.setup.target)}, flat by ${esc(bt.setup.flat_by)}, at most ${bt.setup.max_trades_per_day} entries a session. ${bt.sessions} sessions, ${esc(bt.from)} to ${esc(bt.to)}. The gate stood aside on ${bt.regime_skipped_sessions} of them.</div>` +
      (bt.limits || []).map((l) => `<div class="note warn">${esc(l)}</div>`).join("");
  }
  if (sweep && sweep.rows) {
    html += `<h3 style="margin:18px 0 4px">The whole grid</h3>` + table([
      { label: "Stop", get: (r) => esc(r.stop) },
      { label: "Target", num: true, get: (r) => num(r.target_r, 1) + "R" },
      { label: "Regime gate", get: (r) => r.gate ? '<span class="pill pass">on</span>' : '<span class="pill">off</span>' },
      { label: "Trades", num: true, get: (r) => r.trades },
      { label: "Win rate", num: true, get: (r) => num(r.win_rate_pct, 1) + " %" },
      { label: "Expectancy", num: true, cls: (r) => cls(r.expectancy_r), get: (r) => (r.expectancy_r >= 0 ? "+" : "") + num(r.expectancy_r, 4) + "R" },
      { label: "Total", num: true, cls: (r) => cls(r.total_r), get: (r) => (r.total_r >= 0 ? "+" : "") + num(r.total_r) + "R" },
      { label: "Max DD", num: true, get: (r) => num(r.max_drawdown_r) + "R" },
      { label: "Net", num: true, cls: (r) => cls(r.net), get: (r) => rupees(r.net) },
    ], sweep.rows) +
      `<div class="note"><strong>${esc(sweep.verdict)}</strong></div>
       <div class="note warn">${esc(sweep.warning)}</div>`;
  }
  $("#btOut").innerHTML = html || '<p class="muted small">Nothing run yet.</p>';
}

async function runBacktest(kind, button) {
  const status = $("#btStatus");
  status.className = "status";
  status.textContent = kind === "sweep"
    ? "Running every stop/target combination over 60 days of 5-minute bars. About 40 seconds…"
    : kind === "load" ? "Loading the last run…"
    : "Replaying the setup over 60 days of 5-minute bars across the universe. About 30 seconds…";
  busy(button, true, "running…");
  try {
    if (kind === "load") {
      const saved = await api("/api/backtest/saved");
      renderBacktest(saved.backtest, saved.sweep);
      status.className = "status ok";
      status.textContent = saved.backtest ? "Last run from " + saved.backtest.as_of : "Nothing saved yet.";
    } else if (kind === "sweep") {
      const sweep = await api("/api/sweep", {});
      const saved = await api("/api/backtest/saved");
      renderBacktest(saved.backtest, sweep);
      status.className = "status ok";
      status.textContent = `${sweep.cells_positive} of ${sweep.cells} cells had positive expectancy.`;
    } else {
      const bt = await api("/api/backtest", {});
      renderBacktest(bt, (await api("/api/backtest/saved")).sweep);
      status.className = "status ok";
      status.textContent = `${bt.sessions} sessions, ${bt.ungated.trades || 0} signals.`;
    }
    refreshState();
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy(button, false); }
}

function renderResearch(r) {
  if (!r || !r.results) { $("#resOut").innerHTML = '<p class="muted small">Nothing tested yet.</p>'; return; }
  const sign = (v, d = 4) => v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + num(v, d) + "R";
  $("#resOut").innerHTML = table([
    { label: "Setup", get: (x) => `<strong>${esc(x.name)}</strong><div class="muted small">${esc(x.idea)}</div>` },
    { label: "Family", get: (x) => `<span class="pill">${esc(x.family)}</span>` },
    { label: "Trades", num: true, get: (x) => x.all.trades },
    { label: "Win rate", num: true, get: (x) => x.all.win_rate_pct === null ? "—" : num(x.all.win_rate_pct, 1) + " %" },
    { label: "Expectancy", num: true, cls: (x) => cls(x.all.expectancy_r), get: (x) => sign(x.all.expectancy_r) },
    { label: "Design half", num: true, cls: (x) => cls(x.held_up.arms[0].design_expectancy_r), get: (x) => sign(x.held_up.arms[0].design_expectancy_r) },
    { label: "Confirm half", num: true, cls: (x) => cls(x.held_up.arms[0].confirm_expectancy_r), get: (x) => sign(x.held_up.arms[0].confirm_expectancy_r) },
    { label: "Worst DD", num: true, get: (x) => num(x.all.max_drawdown_r, 1) + "R" },
    { label: "Net", num: true, cls: (x) => cls(x.all.net), get: (x) => rupees(x.all.net) },
    { label: "Held up?", get: (x) => x.held_up.survives ? '<span class="pill pass">survived the split</span>' : '<span class="pill">no</span>' },
  ], r.results) +
    `<div class="note ${r.survivors.length ? "good" : "bad"}"><strong>${esc(r.verdict)}</strong></div>` +
    `<div class="note">${r.sessions} sessions, ${esc(r.from)} to ${esc(r.to)}. Design half to ${esc(r.split.design_to)} (${r.split.design_sessions} sessions), confirm half from ${esc(r.split.confirm_from)} (${r.split.confirm_sessions}). Target ${num(r.target_r, 1)}R.</div>` +
    (r.method || []).map((m) => `<div class="note">${esc(m)}</div>`).join("") +
    (r.limits || []).map((m) => `<div class="note warn">${esc(m)}</div>`).join("");
}

async function runResearch(kind, button) {
  const status = $("#resStatus");
  status.className = "status";
  status.textContent = kind === "load" ? "Loading the last test…"
    : "Running seven setups over 60 days of 5-minute bars across the universe. About 40 seconds…";
  busy(button, true, "testing…");
  try {
    const r = kind === "load" ? (await api("/api/backtest/saved")).research : await api("/api/research", {});
    renderResearch(r);
    status.className = "status ok";
    status.textContent = r ? `${r.results.length} setups, ${r.comparisons} comparisons, ${r.survivors.length} survived the split.`
      : "Nothing saved yet — press “Test the bank”.";
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy(button, false); }
}

/* ------------------------------------------------------------------ movers */

async function runMovers(button, cached) {
  const status = $("#movStatus");
  status.className = "status";
  status.textContent = cached
    ? "Loading the last ranking…"
    : "Pulling 60 days of 5-minute bars for every name. The first run takes about a minute; after that it is cached.";
  busy(button, true, "ranking…");
  try {
    const out = cached
      ? await api("/api/movers/saved")
      : await api("/api/movers", { group: $("#movGroup").value, sessions: Number($("#movSessions").value) });
    renderMovers(out);
    status.className = "status ok";
    status.textContent = out.ranked
      ? `${out.ranked} names ranked on ${out.sessions} sessions (${out.rows[0].from} to ${out.rows[0].to})` +
        (out.failed && out.failed.length ? ` · ${out.failed.length} had too little data` : "") +
        ` · as of ${out.as_of}`
      : "Nothing ranked yet. Press “Rank them”.";
  } catch (err) { status.className = "status err"; status.textContent = err.message; }
  finally { busy(button, false); }
}

let MOVERS = null;

function renderMovers(out) {
  MOVERS = out;
  const key = $("#movSort").value;
  const rows = (out.rows || []).slice().sort((a, b) => (b[key] || 0) - (a[key] || 0));
  if (!rows.length) { $("#movResult").innerHTML = '<p class="muted small">Nothing ranked yet.</p>'; return; }
  const bar = (value, max) => {
    const w = Math.max(2, Math.min(100, (value / max) * 100));
    return `<span style="display:inline-block;width:46px;height:5px;background:#1c2633;border-radius:3px;vertical-align:middle;margin-right:6px">
      <span style="display:block;width:${w}%;height:100%;background:${COLORS.accent};border-radius:3px"></span></span>`;
  };
  const maxScore = Math.max(...rows.map((r) => r.score)) || 1;
  $("#movResult").innerHTML = table([
    { label: "#", num: true, get: (r) => r.rank },
    { label: "Symbol", get: (r) => `<strong>${esc(r.display)}</strong><div class="muted small">${esc(r.name)}</div>` },
    { label: "Score", num: true, get: (r) => `${bar(r.score, maxScore)}<strong>${num(r.score)}</strong>` },
    { label: "Day range", num: true, get: (r) => `${num(r.day_range_pct)} %<div class="muted small">${num(r.range_worst)}–${num(r.range_best)}</div>` },
    { label: "Steadiness", num: true, get: (r) => num(r.steadiness) },
    { label: "Swing", num: true, get: (r) => num(r.swing) },
    { label: "Turns/day", num: true, get: (r) => r.turns },
    { label: "Per 5-min bar", num: true, get: (r) => num(r.bar_move_pct, 3) + " %" },
    { label: "ORB fired", num: true, get: (r) => r.orb_fired + "/" + r.sessions },
    { label: "Target / stop", num: true, get: (r) => r.orb_fired
        ? `<span class="up">${r.orb_target}</span> / <span class="down">${r.orb_stop}</span>` +
          `<div class="muted small">${num(r.orb_target_rate, 0)}% to target</div>`
        : "—" },
    { label: "Last", num: true, get: (r) => num(r.last) },
    { label: "", get: (r) => `<a class="linkish" onclick="pickSymbol('${r.display}')">trade &#9656;</a>` },
  ], rows) + `<div class="note">${esc(out.note || "")}</div><div class="note warn">${esc(out.caveat || "")}</div>`;
}

/* -------------------------------------------------------------------- news */

async function runNews() {
  const symbol = $("#tradeSymbol").value.trim();
  if (!symbol) { toast("Name a symbol first.", "bad"); return; }
  busy($("#newsBtn"), true, "reading…");
  $("#newsOut").innerHTML = '<div class="note">Reading Google News (India) and Yahoo for this name…</div>';
  try {
    const out = await api("/api/news", { symbol, hours: Number($("#newsHours").value) });
    const move = out.move || {};
    stepClass("#step-news", out.headlines.length ? "done" : null);
    const tags = Object.entries(out.tags || {}).map(([tag, n]) => `<span class="pill">${esc(tag)} ${n}</span>`).join(" ");
    $("#newsOut").innerHTML =
      readout([
        { k: "Price", v: num(move.price), n: move.quote_time ? "quote " + move.quote_time.slice(11) : "" },
        { k: "On the day", v: pct(move.change_pct), c: cls(move.change_pct), n: "from the previous close " + num(move.previous_close) },
        { k: "From the open", v: pct(move.from_open_pct), c: cls(move.from_open_pct), n: "open " + num(move.session_open) },
        { k: "Range today", v: num(move.range_pct) + " %", n: `${num(move.day_low)} – ${num(move.day_high)}` },
        { k: "Headlines", v: out.headlines.length, n: "last " + out.hours + " hours" },
      ]) +
      (tags ? `<div style="margin:8px 0">${tags}</div>` : "") +
      out.flags.map((f) => `<div class="note warn">${esc(f)}</div>`).join("") +
      (out.headlines.length ? table([
        { label: "When", get: (r) => `<span class="mono small">${esc(r.published ? r.published.slice(5) : "—")}</span>` +
            (r.age_hours != null ? `<div class="muted small">${num(r.age_hours, 1)}h ago</div>` : "") },
        { label: "Headline", get: (r) => `<a class="linkish" href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.title)}</a>` },
        { label: "Tags", get: (r) => r.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join(" ") },
        { label: "Source", get: (r) => `<span class="muted small">${esc(r.source)}</span>` },
      ], out.headlines) : "") +
      out.errors.map((e) => `<div class="note bad">${esc(e)}</div>`).join("") +
      `<div class="note">${esc(out.note)}</div>`;
  } catch (err) { $("#newsOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#newsBtn"), false); }
}

function pickSymbol(display) {
  $("#tradeSymbol").value = display;
  showTab("trade");
  loadSymbol();
}
window.pickSymbol = pickSymbol;

/* -------------------------------------------------------------- trade desk */

async function loadSymbol() {
  const symbol = $("#tradeSymbol").value.trim();
  if (!symbol) { toast("Name a symbol first.", "bad"); return; }
  busy($("#loadSymbolBtn"), true, "loading…");
  try {
    CHART = await api("/api/chart?symbol=" + encodeURIComponent(symbol) + "&range=1d&interval=5m");
    drawTradeChart();
  } catch (err) { toast(err.message, "bad"); }
  finally { busy($("#loadSymbolBtn"), false); }
}

function drawTradeChart() {
  if (!CHART) return;
  const overlays = [];
  if (CHART.opening_range) {
    overlays.push({ value: CHART.opening_range.high, color: COLORS.warn, label: "OR high " + num(CHART.opening_range.high) });
    overlays.push({ value: CHART.opening_range.low, color: COLORS.warn, label: "OR low " + num(CHART.opening_range.low) });
  }
  const plan = CHART.plan;
  if (plan) {
    overlays.push({ value: plan.entry, color: COLORS.accent, label: "entry " + num(plan.entry), dash: [2, 2] });
    overlays.push({ value: plan.stop, color: COLORS.down, label: "stop " + num(plan.stop) });
    overlays.push({ value: plan.target, color: COLORS.up, label: "target " + num(plan.target) });
  }
  drawCandles($("#priceChart"), CHART.bars, overlays);
  const last = CHART.bars.length ? CHART.bars[CHART.bars.length - 1] : null;
  $("#chartLegend").innerHTML = [
    `<span><strong>${esc(CHART.display)}</strong> ${esc(CHART.name)}</span>`,
    last ? `<span class="mono">last ${num(last.c)} (${esc(last.end)} bar)</span>` : "",
    `<span class="mono">quote ${num(CHART.meta.price)} at ${esc(CHART.meta.quote_time || "—")}</span>`,
    `<span>${CHART.bars.length} five-minute bars</span>`,
    CHART.opening_range ? `<span><i style="background:${COLORS.warn}"></i>opening range ${esc(CHART.opening_range.from)}–${esc(CHART.opening_range.to)}</span>` : "",
    CHART.position ? `<span class="up">holding ${CHART.position.qty}</span>` : "",
    `<span class="muted">${esc(CHART.source)}</span>`,
  ].filter(Boolean).join("");
}

function stepClass(id, kind) {
  const el = $(id);
  el.classList.remove("done", "blocked", "waiting");
  if (kind) el.classList.add(kind);
}

async function runEntry() {
  const symbol = $("#tradeSymbol").value.trim();
  if (!symbol) { toast("Name a symbol first.", "bad"); return; }
  busy($("#entryBtn"), true);
  try {
    const out = await api("/api/entry", {
      symbol, setup: $("#tradeSetup").value,
      level: $("#tradeLevel").value || null,
      min_volume_x: $("#tradeVolX").value || null,
    });
    const triggered = out.status === "triggered";
    stepClass("#step-entry", triggered ? "done" : out.status === "waiting" || out.status === "range_forming" ? "waiting" : "blocked");
    const items = [
      { k: "Status", v: `<span class="pill ${triggered ? "pass" : out.status === "waiting" ? "wait" : "block"}">${esc(out.status)}</span>` },
      { k: "Level to beat", v: num(out.level), n: out.level_label },
    ];
    if (out.opening_range) {
      items.push({ k: "Opening range", v: `${num(out.opening_range.low)} – ${num(out.opening_range.high)}`,
        n: `${out.opening_range.from}–${out.opening_range.to}, ${out.opening_range.bars} bars` });
    }
    if (triggered) {
      items.push({ k: "Entry", v: num(out.entry), c: "up", n: `${out.entry_time}, bar ${out.entry_bar}` });
      items.push({ k: "Over the level", v: pct(out.over_pct), c: "up" });
      if (out.volume_x) items.push({ k: "Volume", v: num(out.volume_x) + "×", n: "vs the range bars' average" });
    } else {
      items.push({ k: "Last close", v: num(out.last_close), n: (out.last_bar || "") + " bar" });
      if (out.distance != null) items.push({ k: "Distance", v: num(out.distance), n: pct(out.distance_pct) + " to go" });
    }
    $("#entryOut").innerHTML = readout(items) +
      `<div class="note ${triggered ? "good" : ""}">${esc(out.message || "")}</div>` +
      (out.notes || []).map((n) => `<div class="note warn">${esc(n)}</div>`).join("");
    if (triggered) await loadSymbol();
    refreshState();
  } catch (err) { $("#entryOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#entryBtn"), false); }
}

async function runPlan() {
  busy($("#planBtn"), true);
  try {
    const out = await api("/api/plan", {
      stop_rule: $("#stopRule").value, target_rule: $("#targetRule").value,
      symbol: $("#tradeSymbol").value.trim() || null,
      entry: $("#planEntry").value || null,
      setup: $("#tradeSetup").value,
    });
    if (out.refused) {
      stepClass("#step-exit", "blocked");
      $("#planOut").innerHTML = `<div class="note bad"><strong>Refused.</strong> ${esc(out.message)}</div>`;
      return;
    }
    stepClass("#step-exit", "done");
    $("#planOut").innerHTML = readout([
      { k: "Entry", v: num(out.entry), n: `${out.entry_time} IST${out.entry_bar ? ", bar " + out.entry_bar : ""}` },
      { k: "Stop", v: num(out.stop), c: "down", n: out.stop_why },
      { k: "Target", v: num(out.target), c: "up", n: out.target_why },
      { k: "Risk a share", v: num(out.risk_per_share), n: pct(out.risk_pct) + " of the entry" },
      { k: "Reward : risk", v: num(out.reward_risk), c: out.reward_risk >= out.min_reward_risk ? "up" : "down", n: "your minimum is " + num(out.min_reward_risk) },
      { k: "Flat by", v: out.flat_by, n: "IST, win or lose" },
    ]) + (out.warning ? `<div class="note bad">${esc(out.warning)}</div>` : "") +
      `<div class="note">${esc(out.note)}</div>`;
    await loadSymbol();
    refreshState();
  } catch (err) { $("#planOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#planBtn"), false); }
}

async function runTimeMachine() {
  busy($("#tmBtn"), true, "replaying 10,000 sessions…");
  $("#tmOut").innerHTML = '<div class="note">Pulling 60 days of 5-minute bars and replaying the rest of the session 10,000 times…</div>';
  try {
    const out = await api("/api/timemachine", {});
    if (out.refused) {
      stepClass("#step-tm", "blocked");
      $("#tmOut").innerHTML = `<div class="note bad"><strong>Refused.</strong> ${esc(out.message)}</div>`;
      return;
    }
    TM = out;
    stepClass("#step-tm", "done");
    const mc = out.mc, h = out.history, plan = out.plan;
    const verdictClass = /toward the target/.test(out.verdict) ? "good" : /toward the stop/.test(out.verdict) ? "bad" : "warn";
    $("#tmOut").innerHTML =
      readout([
        { k: "Target first", v: num(mc.target_first_pct, 1) + " %", c: "up" },
        { k: "Stop first", v: num(mc.stop_first_pct, 1) + " %", c: "down" },
        { k: "Neither", v: num(mc.neither_pct, 1) + " %", n: "still open at " + mc.until },
        { k: "Green", v: num(mc.green_pct, 1) + " %", n: "target first, or above the entry at " + mc.until },
        { k: "Average R", v: (mc.avg_r >= 0 ? "+" : "") + num(mc.avg_r, 3), c: cls(mc.avg_r) },
        { k: "Price at " + mc.until, v: num(mc.median_close), n: `90% between ${num(mc.p5_close)} and ${num(mc.p95_close)}` },
      ]) +
      `<div class="note ${verdictClass}"><strong>${esc(out.verdict)}</strong></div>` +
      out.notes.map((n) => `<div class="note">${esc(n)}</div>`).join("") +
      `<div class="note">History: the same rule on ${h.sessions} real sessions fired ${h.fired} times — ` +
        `${h.target} target, ${h.stop} stop, ${h.neither} neither. Average R ${h.avg_r === null ? "—" : num(h.avg_r, 3)}.</div>` +
      `<div class="grid cols-2" style="margin-top:12px">
         <div><div class="muted small" style="margin-bottom:6px">10,000 replays, 5th–95th and 25th–75th percentile bands, median line</div>
           <canvas id="fanChart" height="220"></canvas></div>
         <div><div class="muted small" style="margin-bottom:6px">Where the price ends up at ${esc(mc.until)}</div>
           <canvas id="histChart" height="220"></canvas></div>
       </div>` +
      `<div class="note">${esc(out.disclaimer)} Pool: ${out.pool_sessions} whole sessions, ${esc(out.pool_from)} to ${esc(out.pool_to)}.</div>`;
    drawFan($("#fanChart"), mc.bands, plan);
    drawHistogram($("#histChart"), mc.histogram, plan);
    refreshState();
  } catch (err) { $("#tmOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#tmBtn"), false); }
}

async function runSize() {
  busy($("#sizeBtn"), true);
  try {
    const out = await api("/api/size", {});
    if (out.refused) {
      stepClass("#step-size", "blocked");
      $("#sizeOut").innerHTML = `<div class="note bad"><strong>Refused, and no order written.</strong> ${esc(out.message)}</div>`;
      return;
    }
    stepClass("#step-size", "done");
    $("#sizeOut").innerHTML = table([
      { label: "Limit", get: (r) => (r.limit === out.decided_by ? `<strong>${esc(r.limit)}</strong>` : esc(r.limit)) },
      { label: "Shares it allows", num: true, get: (r) => (r.limit === out.decided_by ? `<strong>${r.shares}</strong>` : r.shares) },
      { label: "The maths", get: (r) => `<span class="muted small">${esc(r.math)}</span>` },
      { label: "", get: (r) => (r.limit === out.decided_by ? '<span class="pill">decides it</span>' : "") },
    ], out.limits) +
      readout([
        { k: "Shares", v: out.qty },
        { k: "Order value", v: rupees(out.value) },
        { k: "Max loss at the stop", v: rupees(out.max_loss), c: "down" },
        { k: "Reward : risk", v: num(out.reward_risk) },
        { k: "Sized at", v: num(out.entry_used), n: out.entry_why },
      ]) + `<div class="note">${esc(out.note)}</div>`;
    refreshState();
  } catch (err) { $("#sizeOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#sizeBtn"), false); }
}

async function runRisk() {
  busy($("#riskBtn"), true);
  try {
    const out = await api("/api/risk", {});
    const anyBlocked = out.blocked > 0;
    stepClass("#step-risk", anyBlocked ? "blocked" : out.passed ? "done" : null);
    $("#riskOut").innerHTML = out.verdicts.map((v) => `
      <div class="ticket" style="margin-bottom:10px">
        <div class="hd">${v.verdict === "PASS" ? '<span class="pill pass">PASS</span>' : '<span class="pill block">BLOCK</span>'}
          #${v.index} ${esc(v.side)} ${v.qty} ${esc(v.display)} — ${esc(v.kind)}</div>
        <div>entry ${num(v.entry_price)} (${esc(v.entry_why)}), stop ${num(v.stop)}, target ${num(v.target)} · risk ${num(v.risk_per_share)} a share</div>
        <div>max loss ${rupees(v.max_loss)} · reward:risk ${num(v.reward_risk)} · order value ${rupees(v.value)}</div>
        <div class="muted">today before this: ${v.today_before_this.trades} of ${v.today_before_this.max_trades} trades, ${rupees(v.today_before_this.risked)} of ${rupees(v.today_before_this.max_daily_loss)} daily loss</div>
        ${v.reasons.map((r) => `<div class="down">— ${esc(r)}</div>`).join("")}
        ${v.warnings.map((r) => `<div class="warnc">! ${esc(r)}</div>`).join("")}
      </div>`).join("") +
      (out.discipline ? `<div class="note ${out.discipline.regime_allows_entries === false || out.discipline.circuit_breaker !== "TRADING_ALLOWED" ? "warn" : ""}">
       Discipline layer: circuit breaker <strong>${esc(out.discipline.circuit_breaker)}</strong> ·
       portfolio heat ${num(out.discipline.heat_pct)}% of ${rupees(out.discipline.heat_limit)} ·
       regime ${out.discipline.regime ? esc(out.discipline.regime) + " (" + esc(out.discipline.regime_recommendation) + ")" : "not read today"}
       ${out.discipline.revenge_window ? " · <span class=\"down\">inside the revenge window</span>" : ""}</div>` : "") +
    `<div class="note ${anyBlocked ? "bad" : "good"}">${out.passed} passed, ${out.blocked} blocked at ${esc(out.as_of)} IST. ` +
      `Paper cash after the passed orders: ${rupees(out.cash_after_passed)}.</div>` +
      `<div class="note">${esc(out.note)}</div>`;
    if (!out.verdicts.length) $("#riskOut").innerHTML = '<div class="note">No order to check. Size one first.</div>';
    refreshState();
  } catch (err) { $("#riskOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#riskBtn"), false); }
}

async function showTickets() {
  busy($("#ticketBtn"), true);
  try {
    TICKETS = await api("/api/tickets");
    const tickets = TICKETS.tickets || [];
    if (!tickets.length) { $("#ticketOut").innerHTML = '<div class="note">No ticket. Size an order first.</div>'; return; }
    stepClass("#step-ticket", tickets.some((t) => t.state === "READY" || t.state === "PLACED") ? "done" : "blocked");
    $("#ticketOut").innerHTML = tickets.map((t) => {
      const order = t.order;
      const pillClass = t.state === "READY" || t.state === "PLACED" ? "ready" : t.state === "APPROVED" ? "wait" : "blocked";
      return `<div class="ticket">
        <div class="hd">ORDER ${t.index} of ${tickets.length} · PAPER account · built-in simulator
          <span class="pill ${pillClass}">${esc(t.state)}</span></div>
        ${t.lines.map((l) => `<div>${esc(l)}</div>`).join("")}
        <div class="muted">${esc(t.why)}</div>
        <div class="muted small" style="margin-top:8px">the app will fill exactly this, inside the simulated account:</div>
        <div class="muted small">${esc(JSON.stringify({ symbol: order.symbol, side: order.side, qty: String(order.qty), type: order.type, time_in_force: order.time_in_force, order_class: order.order_class, take_profit_limit_price: order.take_profit_limit_price && String(order.take_profit_limit_price), stop_loss_stop_price: order.stop_loss_stop_price && String(order.stop_loss_stop_price) }))}</div>
        ${t.state === "READY" || t.state === "APPROVED"
          ? `<div class="controls" style="margin-top:12px"><button class="primary" onclick="approveTicket(${t.index})">Say yes to this one order</button></div>`
          : ""}
      </div>`;
    }).join("");
  } catch (err) { $("#ticketOut").innerHTML = `<div class="note bad">${esc(err.message)}</div>`; }
  finally { busy($("#ticketBtn"), false); }
}

function approveTicket(index) {
  const ticket = (TICKETS.tickets || []).find((t) => t.index === index);
  if (!ticket) return;
  $("#modalTitle").textContent = "Gate 2 of 3 — type yes for this one order";
  $("#modalBody").innerHTML =
    `<div class="ticket">${ticket.lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>
     <p class="muted small" style="margin-top:10px">The risk check passed this exact order. Type <strong>yes</strong>
     to record your approval for it — nothing vague counts, and there is no "yes to all".</p>
     <input id="yesInput" placeholder="type yes" autocomplete="off" />`;
  $("#modal").classList.remove("hidden");
  setTimeout(() => $("#yesInput") && $("#yesInput").focus(), 50);
  $("#modalOk").textContent = "Record my yes";
  $("#modalOk").onclick = async () => {
    const reply = ($("#yesInput").value || "").trim();
    try {
      const out = await api("/api/approve", { index, reply });
      if (!out.ok) { toast(out.message, "bad"); return; }
      toast(out.message, "good");
      $("#modal").classList.add("hidden");
      confirmFill(index, ticket);
    } catch (err) { toast(err.message, "bad"); }
  };
}
window.approveTicket = approveTicket;

function confirmFill(index, ticket) {
  $("#modalTitle").textContent = "Gate 3 of 3 — fill it in the paper account?";
  $("#modalBody").innerHTML =
    `<div class="ticket">${ticket.lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>
     <p class="muted small" style="margin-top:10px">This fills in the <strong>simulated</strong> account at the
     latest free delayed price. No real order, no broker, no money. Estimated NSE intraday charges come
     out of the paper balance.</p>`;
  $("#modal").classList.remove("hidden");
  $("#modalOk").textContent = "Yes, fill it in the paper account";
  $("#modalOk").onclick = async () => {
    $("#modalOk").disabled = true;
    try {
      const out = await api("/api/place", { index });
      toast(out.message, out.ok ? "good" : "bad");
      $("#modal").classList.add("hidden");
      await showTickets(); await refreshState(); await loadPositions();
    } catch (err) { toast(err.message, "bad"); }
    finally { $("#modalOk").disabled = false; }
  };
}

/* ------------------------------------------------------------- positions */

async function loadPositions() {
  try {
    const [account, history] = await Promise.all([api("/api/account"), api("/api/history")]);
    $("#posNote").textContent = `${account.open_positions} open · equity ${rupees(account.equity)} · as of ${account.as_of} IST`;
    $("#positionsOut").innerHTML = account.positions.length ? table([
      { label: "Symbol", get: (r) => `<strong>${esc(r.display)}</strong><div class="muted small">${esc(r.name)}</div>` },
      { label: "Qty", num: true, get: (r) => r.qty },
      { label: "Avg", num: true, get: (r) => num(r.avg_price) },
      { label: "Last", num: true, get: (r) => num(r.last) },
      { label: "Value", num: true, get: (r) => rupees(r.value) },
      { label: "Unrealised", num: true, cls: (r) => cls(r.unrealised), get: (r) => rupees(r.unrealised) + ` <span class="small">(${pct(r.unrealised_pct)})</span>` },
      { label: "Stop", num: true, get: (r) => `<span class="down">${num(r.stop)}</span><div class="muted small">${num(r.to_stop)} away</div>` },
      { label: "Target", num: true, get: (r) => `<span class="up">${num(r.target)}</span><div class="muted small">${num(r.to_target)} away</div>` },
      { label: "Flat by", get: (r) => esc(r.flat_by || "—") },
      { label: "", get: (r) => `<a class="linkish" onclick="flattenPos('${r.display}',${r.qty})">close ▸</a>` },
    ], account.positions) +
      `<div class="note">The stop and the target are walked bar by bar by the app while the session runs, exactly as a bracket order would be at a broker. A stop fills AT the stop here; a real one can fill worse.</div>`
      : '<p class="muted small">No open positions in the paper account.</p>';

    const stats = history.stats;
    $("#closedOut").innerHTML = (stats.round_trips ? readout([
      { k: "Round trips", v: stats.round_trips },
      { k: "Win rate", v: stats.win_rate === null ? "—" : num(stats.win_rate, 1) + " %", n: `${stats.wins} win · ${stats.losses} loss` },
      { k: "Net", v: rupees(stats.net), c: cls(stats.net) },
      { k: "Best / worst", v: rupees(stats.best) + " / " + rupees(stats.worst) },
    ]) : "") + table([
      { label: "Exit", get: (r) => `<span class="mono small">${esc(r.exit_at.slice(5, 16))}</span>` },
      { label: "Symbol", get: (r) => esc(r.display) },
      { label: "Qty", num: true, get: (r) => r.qty },
      { label: "In", num: true, get: (r) => num(r.entry) },
      { label: "Out", num: true, get: (r) => num(r.exit) },
      { label: "Charges", num: true, get: (r) => rupees(r.charges) },
      { label: "Net", num: true, cls: (r) => cls(r.net), get: (r) => rupees(r.net) },
      { label: "Why it ended", get: (r) => `<span class="muted small">${esc(r.reason || "")}</span>` },
    ], history.closed);

    $("#orderLogOut").innerHTML = table([
      { label: "At", get: (r) => `<span class="mono small">${esc(String(r.at).slice(5, 16))}</span>` },
      { label: "Symbol", get: (r) => esc(r.display || r.symbol) },
      { label: "Side", get: (r) => `<span class="pill ${r.side === "buy" ? "" : "wait"}">${esc(r.side)}</span>` },
      { label: "Qty", num: true, get: (r) => r.qty },
      { label: "Fill", num: true, get: (r) => num(r.fill_price) },
      { label: "Kind", get: (r) => esc(r.kind) + (r.note ? ` <span class="muted small">${esc(r.note)}</span>` : "") },
      { label: "Status", get: (r) => `<span class="pill pass">${esc(r.status)}</span>` },
    ], history.orders);
  } catch (err) { toast(err.message, "bad"); }
}

async function flattenPos(display, qty) {
  if (!confirm(`Write a market sell ticket for ${qty} ${display}? It still goes through the risk check and your yes.`)) return;
  try {
    const out = await api("/api/flatten", { symbol: display, qty });
    toast(out.message, out.ok ? "good" : "bad");
    if (out.ok) { showTab("trade"); await runRisk(); await showTickets(); }
  } catch (err) { toast(err.message, "bad"); }
}
window.flattenPos = flattenPos;

/* ---------------------------------------------------------------- alerts */

async function loadAlerts() {
  try {
    const out = await api("/api/alerts");
    $("#alertsOut").innerHTML = table([
      { label: "Rule", get: (r) => `<span class="mono small">${esc(r.id)}</span>` },
      { label: "Watching", get: (r) => esc(r.display || "the clock") },
      { label: "Type", get: (r) => `<span class="pill">${esc(r.type)}</span>` },
      { label: "Level", num: true, get: (r) => (r.price != null ? num(r.price) : r.pct != null ? num(r.pct) + " %" : esc(r.at || "—")) },
      { label: "Day", get: (r) => esc(r.date || "every day") },
      { label: "State", get: (r) => r.rang_at ? `<span class="pill wait">rang ${esc(r.rang_at.slice(11, 16))}</span>` : r.active ? '<span class="pill pass">watching</span>' : '<span class="pill">idle</span>' },
      { label: "", get: (r) => `<a class="linkish" onclick="removeAlert('${r.id}')">remove</a>` },
    ], out.rules);
  } catch (err) { toast(err.message, "bad"); }
}

async function removeAlert(id) {
  await api("/api/alerts/remove", { id });
  loadAlerts();
}
window.removeAlert = removeAlert;

/* -------------------------------------------------------------- settings */

const CONFIG_FIELDS = [
  ["budget", "Budget (₹)", "number"],
  ["max_loss_per_trade", "Max loss per trade (₹)", "number"],
  ["max_daily_loss", "Max loss for the day (₹)", "number"],
  ["max_trades_per_day", "Entries per day", "number"],
  ["cash_buffer_pct", "Cash buffer (%)", "number"],
  ["max_position_pct", "Max position (% of budget)", "number"],
  ["max_order_value", "Max order value (₹)", "number"],
  ["min_order_value", "Min order value (₹)", "number"],
  ["min_reward_risk", "Minimum reward : risk", "number"],
  ["or_minutes", "Opening range (minutes)", "number"],
  ["no_entries_before", "No entries before (IST)", "text"],
  ["no_entries_after", "No entries after (IST)", "text"],
  ["flat_by", "Flat by (IST)", "text"],
  ["default_target_r", "Default target (R)", "number"],
  ["brokerage_cap", "Brokerage cap per order (₹)", "number"],
  ["require_stop", "Require a stop", "bool"],
  ["apply_costs", "Charge estimated NSE costs", "bool"],
];

async function loadConfig() {
  const cfg = await api("/api/config");
  $("#configForm").innerHTML = CONFIG_FIELDS.map(([key, label, type]) => {
    if (type === "bool") {
      return `<label>${esc(label)}<select data-cfg="${key}" data-type="bool">
        <option value="true" ${cfg[key] ? "selected" : ""}>yes</option>
        <option value="false" ${cfg[key] ? "" : "selected"}>no</option></select></label>`;
    }
    return `<label>${esc(label)}<input data-cfg="${key}" data-type="${type}" value="${esc(cfg[key])}" /></label>`;
  }).join("");
  renderFiles();
}

async function saveConfig() {
  const updates = {};
  $$("[data-cfg]").forEach((el) => {
    const key = el.dataset.cfg, type = el.dataset.type;
    updates[key] = type === "bool" ? el.value === "true" : type === "number" ? Number(el.value) : el.value;
  });
  try {
    await api("/api/config", updates);
    $("#configStatus").className = "status ok";
    $("#configStatus").textContent = "Saved. Every step of the chain reads these.";
    refreshState();
  } catch (err) { $("#configStatus").className = "status err"; $("#configStatus").textContent = err.message; }
}

function renderAccountBox() {
  if (!STATE) return;
  const a = STATE.account;
  $("#accountBox").innerHTML = readout([
    { k: "Account", v: `<span style="font-size:13px">${esc(a.account)}</span>`, n: esc(a.kind) },
    { k: "Opening balance", v: rupees(a.opening_balance) },
    { k: "Cash", v: rupees(a.cash) },
    { k: "Equity", v: rupees(a.equity), c: cls(a.total_return) },
    { k: "Realised", v: rupees(a.realised_pnl), c: cls(a.realised_pnl) },
    { k: "Charges paid", v: rupees(a.charges_paid) },
  ]);
}

async function renderFiles() {
  try {
    const files = await api("/api/files");
    $("#filesOut").innerHTML = table([
      { label: "File", get: (r) => `<span class="mono small">trader/${esc(r.name)}</span>` },
      { label: "Written", get: (r) => (r.exists ? '<span class="pill pass">yes</span>' : '<span class="pill">not yet</span>') },
      { label: "What is in it", get: (r) => `<span class="muted small">${esc(JSON.stringify(r.preview || "").slice(0, 170))}</span>` },
    ], Object.entries(files).map(([name, info]) => ({ name, ...info })));
  } catch (err) { $("#filesOut").textContent = err.message; }
}

/* ------------------------------------------------------------------ tabs */

function showTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
  if (name === "dashboard") { loadIndexChart(); refreshState(); loadWatchSaved(); }
  if (name === "positions") loadPositions();
  if (name === "alerts") loadAlerts();
  if (name === "settings") loadConfig();
  if (name === "trade" && CHART) drawTradeChart();
  if (name === "feed") loadFeed();
  if (name === "invest") {
    if (!$("#ltOut").innerHTML) runLongtest("load", $("#ltLoadBtn"));
    if (!$("#tgOut").innerHTML) buildTargets(true, $("#tgLoadBtn"));
    loadTaxLots(false, $("#txBtn"));
  }
  if (name === "movers" && !$("#movResult").innerHTML) runMovers($("#movLoadBtn"), true);
  if (name === "discipline") {
    loadDiscipline(); loadPerformance();
    if (!$("#regimeOut").innerHTML) runRegime(false);
    if (!$("#btOut").innerHTML) runBacktest("load", $("#btLoadBtn"));
    if (!$("#resOut").innerHTML) runResearch("load", $("#resLoadBtn"));
  }
}

/* ------------------------------------------------------------------- wire */

function wire() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  $("#refreshBtn").addEventListener("click", () => { refreshState(); loadIndexChart(); });
  $("#tickBtn").addEventListener("click", async () => {
    const out = await api("/api/tick", { force: true });
    toast(out.events.length ? out.events.map((e) => `${e.display} ${e.event} at ${num(e.price)}`).join("; ")
      : "Nothing was hit. Market is " + out.market + ".", out.events.length ? "good" : "");
    refreshState(); loadPositions();
  });
  $("#scanOrbBtn").addEventListener("click", (e) => runScan("orb", e.target));
  $("#scanDailyBtn").addEventListener("click", (e) => runScan("daily", e.target));
  $("#loadSymbolBtn").addEventListener("click", loadSymbol);
  $("#tradeSymbol").addEventListener("keydown", (e) => { if (e.key === "Enter") loadSymbol(); });
  $("#tradeSetup").addEventListener("change", (e) => {
    $("#levelWrap").classList.toggle("hidden", e.target.value !== "level");
  });
  $("#ltRunBtn").addEventListener("click", (e) => runLongtest("run", e.target));
  $("#ltLoadBtn").addEventListener("click", (e) => runLongtest("load", e.target));
  $("#tgBuildBtn").addEventListener("click", (e) => buildTargets(false, e.target));
  $("#tgLoadBtn").addEventListener("click", (e) => buildTargets(true, e.target));
  $("#rbDriftBtn").addEventListener("click", (e) => showDrift(false, e.target));
  $("#rbWriteBtn").addEventListener("click", (e) => showDrift(true, e.target));
  $("#rbBasketBtn").addEventListener("click", (e) => approveBasket(e.target));
  $("#txBtn").addEventListener("click", (e) => loadTaxLots(false, e.target));
  $("#txHarvestBtn").addEventListener("click", (e) => loadTaxLots(true, e.target));
  $("#feedPollBtn").addEventListener("click", pollFeed);
  $("#feedReadBtn").addEventListener("click", async () => { await api("/api/feed/read", {}); loadFeed(); });
  $("#feedFilterBtn").addEventListener("click", () => {
    FEED_FILTER = { symbol: $("#feedSymbol").value.trim() || null, tag: $("#feedTag").value || null };
    loadFeed();
  });
  $("#feedClearBtn").addEventListener("click", () => {
    FEED_FILTER = { symbol: null, tag: null };
    $("#feedSymbol").value = ""; $("#feedTag").value = "";
    loadFeed();
  });
  $("#watchBtn").addEventListener("click", () => runWatch(true));
  $("#regimeBtn").addEventListener("click", () => runRegime(true));
  $("#disciplineBtn").addEventListener("click", loadDiscipline);
  $("#perfBtn").addEventListener("click", loadPerformance);
  $("#btRunBtn").addEventListener("click", (e) => runBacktest("run", e.target));
  $("#btSweepBtn").addEventListener("click", (e) => runBacktest("sweep", e.target));
  $("#btLoadBtn").addEventListener("click", (e) => runBacktest("load", e.target));
  $("#resRunBtn").addEventListener("click", (e) => runResearch("run", e.target));
  $("#resLoadBtn").addEventListener("click", (e) => runResearch("load", e.target));
  $("#movRankBtn").addEventListener("click", (e) => runMovers(e.target, false));
  $("#movLoadBtn").addEventListener("click", (e) => runMovers(e.target, true));
  $("#movSort").addEventListener("change", () => { if (MOVERS) renderMovers(MOVERS); });
  $("#newsBtn").addEventListener("click", runNews);
  $("#entryBtn").addEventListener("click", runEntry);
  $("#planBtn").addEventListener("click", runPlan);
  $("#tmBtn").addEventListener("click", runTimeMachine);
  $("#sizeBtn").addEventListener("click", runSize);
  $("#riskBtn").addEventListener("click", runRisk);
  $("#ticketBtn").addEventListener("click", showTickets);
  $("#saveConfigBtn").addEventListener("click", saveConfig);
  $("#resetBtn").addEventListener("click", async () => {
    if (!confirm("Open a brand-new paper account? The simulated cash, positions, trades and the chain's working files are wiped.")) return;
    const out = await api("/api/account/reset", { opening: Number($("#resetAmount").value) });
    toast("Fresh paper account " + out.account + ".", "good");
    refreshState(); loadPositions();
  });
  $("#alertFromPlanBtn").addEventListener("click", async () => {
    const out = await api("/api/alerts/from-plan", {});
    $("#alertStatus").className = "status " + (out.ok ? "ok" : "err");
    $("#alertStatus").textContent = out.message;
    loadAlerts();
  });
  $("#alertCheckBtn").addEventListener("click", async () => {
    const out = await api("/api/alerts/check", {});
    $("#alertStatus").className = "status ok";
    $("#alertStatus").textContent = out.message + " " + (out.fired.map((f) => f.text).join(" ") || "");
    loadAlerts();
  });
  $("#alertAddBtn").addEventListener("click", async () => {
    const type = $("#alertType").value;
    const value = $("#alertValue").value.trim();
    const rule = { type, symbol: $("#alertSymbol").value.trim() || null };
    if (type === "time") rule.at = value;
    else if (type === "pct_move") rule.pct = Number(value);
    else if (type !== "opening_bell") rule.price = Number(value);
    const out = await api("/api/alerts/add", rule);
    $("#alertStatus").className = "status " + (out.ok ? "ok" : "err");
    $("#alertStatus").textContent = out.message;
    loadAlerts();
  });
  $("#modalCancel").addEventListener("click", () => $("#modal").classList.add("hidden"));
  window.addEventListener("resize", () => {
    if (STATE) {
      drawLine($("#equityChart"), (STATE.account.equity_curve || []).map((p) => ({ t: p.t.slice(11), v: p.equity })),
        { base: STATE.account.opening_balance });
    }
    if (CHART) drawTradeChart();
    if (TM) { drawFan($("#fanChart"), TM.mc.bands, TM.plan); drawHistogram($("#histChart"), TM.mc.histogram, TM.plan); }
  });
}

async function fillSymbolList() {
  try {
    const out = await api("/api/universe");
    const all = [...out.groups.nifty50, ...out.groups.next50];
    $("#symbolList").innerHTML = all.map((s) => `<option value="${esc(s.display)}">${esc(s.name)}</option>`).join("");
  } catch (err) { /* the list is a convenience, not a requirement */ }
}

(async function start() {
  wire();
  drawCandles($("#priceChart"), []);
  await refreshState();
  fillSymbolList();
  loadIndexChart();
  loadWatchSaved();
  updateFeedBadge();
  setInterval(refreshState, 25000);
  setInterval(loadWatchSaved, 60000);
  setInterval(() => {
    if ($("#page-feed").classList.contains("active")) loadFeed();
    else updateFeedBadge();
  }, 45000);
})();
