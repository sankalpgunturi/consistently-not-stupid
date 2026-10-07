const $ = (id) => document.getElementById(id);

const money = (value, digits = 2) => {
  const n = Number(value || 0);
  const sign = n < 0 ? "−" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
};

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[ch]));

const countdown = (iso) => {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return "—";
  if (ms <= 0) return "due";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${m}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
};

let state = null;
let lastStamp = "";

function render(next) {
  state = next;
  const book = next.book || {};
  const status = next.status === "scanning" ? "Scanning" : next.status === "paused" ? "Paused" : "Paper";
  $("mode").textContent = status;
  $("live-mode").textContent = "Live unavailable";
  $("clock").textContent = new Date(next.server_time || Date.now()).toLocaleTimeString();
  $("next").textContent = next.status === "scanning" ? "Reading the books" : `Next scan ${countdown(next.next_scan_at)}`;
  $("pause").textContent = next.operator_pause ? "Resume buys" : "Pause buys";
  const stamp = JSON.stringify({
    status: next.status,
    operator_pause: next.operator_pause,
    paper_age_days: next.paper_age_days,
    evidence: next.evidence,
    book,
    counts: next.counts,
    tape: next.tape,
    positions: next.positions,
    trades: next.trades,
    focus: next.focus,
    retro: next.retrospective,
    sim: Boolean(next.simulation && next.simulation.books),
    cycle: next.cycle,
    params: next.params,
    audit: next.audit,
  });
  if (stamp === lastStamp) return;
  lastStamp = stamp;

  const cards = [
    ["Equity", money(book.equity), book.equity >= book.start ? "up" : "down"],
    ["Cash", money(book.cash), ""],
    ["At work", money(book.deployed), ""],
    ["Realized", money(book.realized), book.realized > 0 ? "up" : book.realized < 0 ? "down" : ""],
    ["Unrealized", money(book.unrealized), book.unrealized > 0 ? "up" : book.unrealized < 0 ? "down" : ""],
    ["Fees paid", money(book.fees_paid, 2), ""],
  ];
  $("stats").innerHTML = cards.map(([label, value, tone]) =>
    `<div class="stat ${tone}"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`
  ).join("");

  renderEvidence(next);
  const old = document.querySelector(".banner");
  if (old) old.remove();
  const messages = [];
  if (book.drawdown_pause) messages.push("New buys are paused. The book is under its peak by the pause line. Exits still run.");
  if (next.operator_pause) messages.push("New buys are paused by you. Exits still run.");
  (next.errors || []).forEach((item) => messages.push(item));
  ((next.evidence || {}).errors || []).forEach((item) => {
    if (!messages.includes(item)) messages.push(item);
  });
  if (messages.length) {
    const banner = document.createElement("div");
    banner.className = "banner";
    banner.textContent = messages.join(" ");
    document.querySelector(".thesis").after(banner);
  }

  const counts = next.counts || {};
  const steps = [
    [counts.markets_read, "markets read"],
    [counts.favorites, "at the bar"],
    [counts.fee_ok, "fee still leaves a profit"],
    [counts.stable, "held still"],
    [counts.confirmed, "second price"],
    [counts.candidates, "awaiting you"],
    [counts.bought, "bought"],
  ];
  $("funnel").innerHTML = steps.map(([value, label]) =>
    `<div class="step"><b>${esc(value ?? "—")}</b><span>${esc(label)}</span></div>`
  ).join("");

  const focus = next.focus;
  $("focus-title").textContent = focus ? focus.title : "Waiting for the first scan";
  $("focus-detail").textContent = focus
    ? `${focus.venue ? focus.venue + " · " : ""}${focus.outcome ? focus.outcome + ". " : ""}${focus.detail || ""}`
    : "The desk is about to read both books.";

  drawEquity(next.equity_curve || [], book.start || 1000);
  renderPositions(next.positions || []);
  renderTape(next.tape || [], next.cycle || {});
  renderRetro(next.retrospective, next.llm || {});
  renderModel(next.simulation);
  renderKnobs(next.params || []);
  renderTrades(next.trades || []);
  renderAudit(next.audit || []);
}

function renderEvidence(next) {
  const evidence = next.evidence || {};
  const day = Math.floor(Number(next.paper_age_days || 0)) + 1;
  const hit = evidence.hit_rate == null ? "—" : `${Math.round(evidence.hit_rate * 1000) / 10}%`;
  const errors = (evidence.errors || []).length;
  const cells = [
    [`Day ${day}`, "of the paper window"],
    [money(evidence.pnl_after_fees), "net P&L after fees"],
    [String(evidence.resolved_count ?? 0), "resolved"],
    [hit, "hit rate"],
    [`${Math.round((evidence.drawdown || 0) * 1000) / 10}%`, "drawdown"],
    [money(evidence.unresolved_cost), errors ? `unresolved · ${errors} venue error${errors === 1 ? "" : "s"}` : "unresolved cost"],
  ];
  $("evidence").innerHTML = cells.map(([value, label]) =>
    `<div><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`
  ).join("");
}

function renderPositions(rows) {
  $("position-count").textContent = rows.length ? `${rows.length} open` : "Cash is the position.";
  $("positions-empty").classList.toggle("hidden", rows.length > 0);
  $("positions").innerHTML = rows.map((row) => `
    <tr>
      <td>
        <span class="venue">${esc(row.venue)}</span>
        <span class="title">${esc(row.title)}</span>
        <span class="sub">${esc(row.outcome)} · ${esc(row.shares)} shares · ${esc(row.signal)}</span>
      </td>
      <td class="num">${esc(money(row.cost_basis))}</td>
      <td class="num">${esc(money(row.mark))}</td>
      <td class="num good">${esc(money(row.profit_if_win))}</td>
      <td class="num bad">${esc(money(-row.loss_if_wrong))}</td>
      <td><button type="button" class="mini" data-close="${esc(row.id)}">Close</button></td>
    </tr>
  `).join("");
}

function renderTape(rows, cycle) {
  const meta = [];
  if (cycle.number) meta.push(`Scan ${cycle.number}`);
  if (cycle.duration_seconds != null) meta.push(`${cycle.duration_seconds}s`);
  if (cycle.finished_at) meta.push(new Date(cycle.finished_at).toLocaleTimeString());
  $("scan-meta").textContent = meta.join(" · ") || "—";
  $("tape-empty").classList.toggle("hidden", rows.length > 0);
  $("tape").innerHTML = rows.slice(0, 40).map((row) => `
    <li>
      <span class="tag ${esc(row.action)}">${esc(row.action)}${row.group_count > 1 ? ` ×${row.group_count}` : ""}</span>
      <div>
        <span class="venue">${esc(row.venue)}</span>
        <span class="title">${esc(row.title)}</span>
        <span class="sub">${esc(row.outcome || "")}${row.price != null ? ` · ${Math.round(row.price * 1000) / 10}¢` : ""}</span>
        <span class="sub">${esc(row.detail || "")}</span>
        ${rowActions(row)}
      </div>
    </li>
  `).join("");
}

function rowActions(row) {
  const bits = [];
  if (row.reason_code === "equivalence" && row.pair_id) {
    bits.push(`<input class="note" data-note="${esc(row.pair_id)}" placeholder="What you checked in the rules" />`);
    bits.push(`<button type="button" class="mini" data-approve="${esc(row.pair_id)}">Approve pair</button>`);
  }
  if (row.key) bits.push(`<button type="button" class="mini" data-block="${esc(row.key)}">Block</button>`);
  if (!bits.length) return "";
  return `<div class="row-actions">${bits.join("")}</div>`;
}

function renderRetro(retro, llm) {
  $("retro-source").textContent = llm.enabled ? `OpenAI ${llm.model} · governor still decides` : "Built-in governor";
  $("retro").textContent = retro?.summary || "The note appears after the first pass.";
  $("retro-notes").innerHTML = (retro?.notes || []).map((note) => `<li>${esc(note)}</li>`).join("");
}

function renderModel(sim) {
  if (!sim || !sim.books) {
    $("model-note").textContent = "The model is running.";
    return;
  }
  $("model-body").innerHTML = sim.books.map((book) => `
    <tr>
      <td>${esc(book.name)}</td>
      <td>${esc(book.world)}</td>
      <td class="num">${esc(book.mean_trades)}</td>
      <td class="num ${book.mean_pnl < 0 ? "bad" : book.mean_pnl > 0 ? "good" : ""}">${esc(money(book.mean_pnl))}</td>
      <td class="num">${esc(money(book.mean_fees))}</td>
      <td class="num">${esc(Math.round(book.paths_under_start * 100))}%</td>
    </tr>
  `).join("");
  $("model-note").textContent = `${sim.note || ""} On the chart, red buys every favorite, cream is this desk when the quote is right, and green is this desk when a second venue is higher.`;
  drawModel(sim);
}

function renderKnobs(rows) {
  $("knobs").innerHTML = rows.map((row) => `
    <div class="knob">
      <b>${esc(row.label)}</b>
      <em>${esc(formatKnob(row))}</em>
      <small>${esc(row.help)} Rail ${esc(row.min)}–${esc(row.max)}.</small>
      ${row.adjustable === false ? "" : `<button type="button" class="mini" data-tighten="${esc(row.key)}">Tighten</button>`}
    </div>
  `).join("");
}

function formatKnob(row) {
  const value = Number(row.value);
  const cents = new Set(["min_edge", "min_win_profit", "max_spread", "stop_gap"]);
  const percent = new Set([
    "min_probability", "max_position_fraction", "max_deployed_fraction",
    "max_category_fraction", "max_drawdown", "match_similarity", "correlation_threshold",
  ]);
  if (cents.has(row.key)) return `${(value * 100).toFixed(1)}¢`;
  if (percent.has(row.key)) return `${(value * 100).toFixed(1)}%`;
  if (row.key === "scan_interval_seconds") return `${Math.round(value / 60)} min`;
  if (row.key === "min_hours_to_expiry") return `${value} hours`;
  if (row.key === "max_days_to_expiry") return `${value} days`;
  if (row.key === "min_liquidity") return money(value, 0);
  return String(row.value);
}

function renderAudit(rows) {
  $("audit-empty").classList.toggle("hidden", rows.length > 0);
  $("audit").innerHTML = rows.map((row) =>
    `<li>${esc(new Date(row.ts).toLocaleString())} · ${esc(row.actor)} ${esc(row.action)} · ${esc(row.reason)}</li>`
  ).join("");
}

function renderTrades(rows) {
  $("trades-empty").classList.toggle("hidden", rows.length > 0);
  $("trades").innerHTML = rows.map((row) => `
    <tr>
      <td class="num">${esc(new Date(row.ts).toLocaleString())}</td>
      <td>
        <span class="venue">${esc(row.action)} · ${esc(row.venue)}</span>
        <span class="title">${esc(row.title)}</span>
      </td>
      <td class="num">${esc(money(row.price))}</td>
      <td class="num">${esc(money(row.fee))}</td>
      <td class="num ${row.pnl < 0 ? "bad" : row.pnl > 0 ? "good" : ""}">${row.action === "buy" ? "—" : esc(money(row.pnl))}</td>
    </tr>
  `).join("");
}

function drawEquity(points, start) {
  const canvas = $("equity");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const series = points.length ? points.map((p) => p.equity) : [start];
  const min = Math.min(start, ...series) - 2;
  const max = Math.max(start, ...series) + 2;
  const x = (i) => 16 + (i * (w - 32)) / Math.max(1, series.length - 1);
  const y = (v) => 16 + (1 - (v - min) / (max - min)) * (h - 32);
  ctx.strokeStyle = "rgba(243,234,215,0.25)";
  ctx.setLineDash([4, 6]);
  ctx.beginPath();
  ctx.moveTo(16, y(start));
  ctx.lineTo(w - 16, y(start));
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = "#e0b56a";
  ctx.lineWidth = 2;
  ctx.beginPath();
  series.forEach((value, i) => (i ? ctx.lineTo(x(i), y(value)) : ctx.moveTo(x(i), y(value))));
  ctx.stroke();
}

function drawModel(sim) {
  const canvas = $("model-chart");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const lines = [
    [sim.curves.naive_fair, "#e58972"],
    [sim.curves.common_fair, "#f4ecdf"],
    [sim.curves.common_dislocated, "#9dcc86"],
  ];
  const all = lines.flatMap(([series]) => series || []);
  if (!all.length) return;
  const min = Math.min(...all) - 1;
  const max = Math.max(...all) + 1;
  const y = (v) => 16 + (1 - (v - min) / (max - min)) * (h - 32);
  lines.forEach(([series, color]) => {
    if (!series) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    series.forEach((value, i) => {
      const px = 16 + (i * (w - 32)) / Math.max(1, series.length - 1);
      i ? ctx.lineTo(px, y(value)) : ctx.moveTo(px, y(value));
    });
    ctx.stroke();
  });
}

async function pull() {
  const response = await fetch("/api/state");
  render(await response.json());
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  socket.onopen = () => {
    $("live").classList.add("on");
    $("live").querySelector("span").textContent = "Connected";
  };
  socket.onmessage = (event) => render(JSON.parse(event.data));
  socket.onclose = () => {
    $("live").classList.remove("on");
    $("live").querySelector("span").textContent = "Reconnecting";
    setTimeout(connect, 1500);
  };
}

async function post(url, body) {
  const headers = { "X-CSRF-Token": state?.csrf || "" };
  const options = { method: "POST", headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  let payload = null;
  try { payload = await response.json(); } catch (_err) { payload = null; }
  if (response.status === 202) return payload;
  if (!response.ok) {
    const message = payload?.error || "The desk refused that change.";
    window.alert(message);
    if (payload?.state) render(payload.state);
    return null;
  }
  if (payload) render(payload);
  return payload;
}

$("scan").addEventListener("click", async () => {
  $("scan").disabled = true;
  $("scan").textContent = "Scanning";
  try {
    await post("/api/scan");
  } finally {
    $("scan").disabled = false;
    $("scan").textContent = "Scan now";
  }
});

$("pause").addEventListener("click", () => {
  post("/api/pause", { paused: !state?.operator_pause });
});

$("reset").addEventListener("click", async () => {
  if (!confirm("Reset the paper book to the starting cash? The tape, the open trades, and the audit will be cleared.")) return;
  await post("/api/reset");
});

document.body.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  if (target.dataset.approve) {
    const note = document.querySelector(`[data-note="${CSS.escape(target.dataset.approve)}"]`);
    const text = note ? note.value.trim() : "";
    if (!text) {
      window.alert("Write what you checked in the resolution rules before approving the pair.");
      return;
    }
    await post("/api/approve-pair", { pair_id: target.dataset.approve, note: text });
  } else if (target.dataset.block) {
    await post("/api/block", { key: target.dataset.block });
  } else if (target.dataset.close) {
    if (!confirm("Close this paper clip at the current bid, if the book can fill it?")) return;
    await post("/api/close", { id: target.dataset.close });
  } else if (target.dataset.tighten) {
    await post("/api/knob", { key: target.dataset.tighten });
  }
});

setInterval(() => {
  if (state) $("next").textContent = state.status === "scanning" ? "Reading the books" : `Next scan ${countdown(state.next_scan_at)}`;
}, 1000);

pull().catch(() => {});
connect();
