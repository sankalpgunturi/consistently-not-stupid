const $ = (id) => document.getElementById(id);

const money = (value, digits = 2) => {
  const n = Number(value || 0);
  const sign = n < 0 ? "−" : "";
  return sign + "$" + (Math.round(Math.abs(n) * 10 ** digits + 1e-8) / 10 ** digits).toLocaleString("en-US", {
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
  $("next").textContent = next.status === "scanning" ? "Reading the books" : `Next scan ${countdown(next.next_scan_at)}`;
  $("pause").textContent = next.operator_pause ? "Resume buys" : "Pause buys";
  const stamp = JSON.stringify({
    status: next.status,
    operator_pause: next.operator_pause,
    paper_age_days: next.paper_age_days,
    evidence: next.evidence,
    book,
    daily: next.daily,
    last_24h: next.last_24h,
    benchmark: next.benchmark,
    counts: next.counts,
    tape: next.tape,
    positions: next.positions,
    trades: next.trades,
    realized_curve: next.realized_curve,
    focus: next.focus,
    retro: next.retrospective,
    cycle: next.cycle,
    params: next.params,
    audit: next.audit,
    research: next.research,
  });
  if (stamp === lastStamp) return;
  lastStamp = stamp;

  const totalGain = (book.equity || 0) - (book.start || 0);
  const recent = next.last_24h || {};
  const tone = (value) => value > 0 ? "up" : value < 0 ? "down" : "";
  const cards = [
    ["Account value", money(book.equity), "", ""],
    ["Total profit", money(totalGain), tone(totalGain), ""],
    ["Last 24 hours", money(recent.net_pnl || 0), tone(recent.net_pnl || 0),
      ""],
  ];
  $("stats").innerHTML = cards.map(([label, value, color, detail]) =>
    `<div class="stat ${color}"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></div>`
  ).join("");

  const benchmark = next.benchmark || {};
  $("benchmark").textContent = `S&P 500 equivalent: ${benchmark.value == null ? "—" : money(benchmark.value)}`;
  $("benchmark-date").textContent = benchmark.as_of ? `As of ${new Date(benchmark.as_of).toLocaleDateString([], {month:"short", day:"numeric"})} close${benchmark.error ? " · Update unavailable" : ""}` : "";
  $("benchmark").title = benchmark.basis || "Adjusted daily close";
  renderEvidence(next);
  renderResearch(next.research || {});
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
  $("run-summary").textContent = next.status === "error" ? "Scan failed" :
    book.entries_paused ? "Paused" : next.status === "scanning" ? "Scanning" : "Running";
  const steps = [
    [counts.markets_read, "markets read"],
    [counts.favorites, "favorites"],
    [counts.fee_ok, "after fees"],
    [counts.stable, "stable"],
    [counts.confirmed, "entry checks passed"],
    [counts.bought, "filled"],
  ];
  $("funnel").innerHTML = steps.map(([value, label]) =>
    `<div class="step"><b>${esc(value ?? "—")}</b><span>${esc(label)}</span></div>`
  ).join("");

  const focus = next.focus;
  $("focus-title").textContent = focus ? focus.title : "Waiting for the first scan";
  $("focus-detail").textContent = focus
    ? `${focus.venue ? focus.venue + " · " : ""}${focus.outcome ? focus.outcome + ". " : ""}${focus.detail || ""}`
    : "The desk is about to read Kalshi.";

  drawProfit(next.realized_curve || []);
  renderPositions(next.positions || []);
  renderTape(next.tape || [], next.cycle || {});
  renderRetro(next.retrospective, next.llm || {});
  renderKnobs(next.params || []);
  renderTrades(next.trades || []);
  renderAudit(next.audit || []);
}

function renderEvidence(next) {
  const evidence = next.evidence || {};
  const book = next.book || {};
  const cells = [
    [money(book.cash), "cash"],
    [money(book.deployed), "invested"],
    [money(book.realized), "realized P&L"],
    [money(book.unrealized), "unrealized P&L"],
    [money(book.fees_paid), "fees"],
    [`${Math.round((evidence.drawdown || 0) * 1000) / 10}%`, "drawdown"],
  ];
  $("evidence").innerHTML = cells.map(([value, label]) =>
    `<div><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`
  ).join("");
}

function renderPositions(rows) {
  $("position-count").textContent = `${rows.length} open`;
  $("positions-empty").classList.toggle("hidden", rows.length > 0);
  $("positions").closest(".table-wrap").classList.toggle("hidden", rows.length === 0);
  $("positions").innerHTML = rows.map((row) => `
    <tr>
      <td>
        <span class="title">${esc(row.title)}</span>
        <span class="sub">${esc(row.side?.toUpperCase())} · ${esc(row.shares)} contracts</span>
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
  const drafts = new Map();
  let focused = null;
  let caret = null;
  document.querySelectorAll("[data-note]").forEach((el) => {
    drafts.set(el.dataset.note, el.value);
    if (document.activeElement === el) {
      focused = el.dataset.note;
      caret = el.selectionStart;
    }
  });
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
  document.querySelectorAll("[data-note]").forEach((el) => {
    if (drafts.has(el.dataset.note)) el.value = drafts.get(el.dataset.note);
  });
  if (focused) {
    const again = document.querySelector(`[data-note="${CSS.escape(focused)}"]`);
    if (again) {
      again.focus();
      const pos = caret == null ? again.value.length : caret;
      again.setSelectionRange(pos, pos);
    }
  }
}

function rowActions(row) {
  const bits = [];
  if (row.key && !(row.group_count > 1)) {
    bits.push(`<button type="button" class="mini" data-block="${esc(row.key)}">Block</button>`);
  }
  if (!bits.length) return "";
  return `<div class="row-actions">${bits.join("")}</div>`;
}

function renderRetro(retro, llm) {
  $("retro-source").textContent = "";
  $("retro").textContent = retro?.summary || "The note appears after the first pass.";
  const notes = [...(retro?.notes || []), ...(retro?.concerns || [])];
  if (retro?.model_error) notes.push(retro.model_error);
  $("retro-notes").innerHTML = notes.map((note) => `<li>${esc(note)}</li>`).join("");
}

function renderResearch(research) {
  $("research-counts").textContent = `${research.scans_recorded || 0} scans archived · ${research.observations || 0} quote observations · ${research.distinct_contract_sides || 0} distinct contract sides`;
  const near = research.near_resolution;
  if (near) $("research-counts").textContent += ` · ${near.observed_events} near-outcome events observed, ${near.resolved_events} resolved`;
  const rows = research.calibration || [];
  $("calibration").innerHTML = rows.map(row => `<tr><td>${esc(row.bucket)}</td><td>${esc(row.samples)}</td><td>${esc(row.wins)}</td><td>${esc((row.lower_bound * 100).toFixed(1))}%</td></tr>`).join("");
  $("research-note").textContent = "";
}

function renderKnobs(rows) {
  $("knobs").innerHTML = rows.map((row) => `
    <div class="knob">
      <b>${esc(row.label)}</b>
      <em>${esc(formatKnob(row))}</em>

      ${row.adjustable === false ? "" : `<button type="button" class="mini" data-tighten="${esc(row.key)}">Tighten</button>`}
    </div>
  `).join("");
}

function formatKnob(row) {
  const value = Number(row.value);
  const cents = new Set(["min_edge", "min_win_profit", "max_spread", "stop_gap"]);
  const percent = new Set([
    "min_probability", "max_position_fraction", "max_deployed_fraction",
    "max_category_fraction", "max_drawdown", "correlation_threshold",
  ]);
  if (cents.has(row.key)) return `${(value * 100).toFixed(1)}¢`;
  if (percent.has(row.key)) return `${(value * 100).toFixed(1)}%`;
  if (row.key === "entry_window_minutes") return `${value} min`;
  if (row.key === "scan_interval_seconds") return `${Math.round(value / 60)} min`;
  if (row.key === "min_hours_to_expiry") return `${value} hours`;
  if (row.key === "max_days_to_expiry") return `${value} days`;
  return String(row.value);
}

function renderAudit(rows) {
  $("audit-empty").classList.toggle("hidden", rows.length > 0);
  $("audit").innerHTML = rows.map((row) =>
    `<li>${esc(new Date(row.ts).toLocaleString())} · ${esc(row.actor)} ${esc(row.action)} · ${esc(row.reason)}</li>`
  ).join("");
}

function renderTrades(rows) {
  const stories = [];
  const pending = new Map();
  for (const row of [...rows].reverse()) {
    const key = `${row.venue}:${row.market_id}`;
    if (row.action === "buy") {
      const story = {entry: row, exit: null};
      stories.push(story);
      pending.set(key, story);
    } else {
      const story = pending.get(key);
      if (story) { story.exit = row; pending.delete(key); }
      else stories.push({entry: null, exit: row});
    }
  }
  const time = (ts) => new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  const cents = (n) => `${Number((n * 100).toFixed(3))}¢`;
  $("trades-empty").classList.toggle("hidden", stories.length > 0);
  $("trades").innerHTML = stories.reverse().slice(0, 8).map(({entry, exit}) => {
    const row = entry || exit;
    const stopped = exit?.action === "sell" && exit.reason?.includes("bid fell");
    const result = !exit ? "Open" : exit.action === "settle" ? (exit.won ? "Won" : "Lost") : stopped ? "Stop-loss" : "Sold";
    const paid = entry ? cents(entry.shares * entry.price + entry.fee) : "—";
    const outcomeClass = !exit ? "" : exit.pnl > 0 ? "trade-win" : exit.pnl < 0 ? "trade-loss" : "";
    const outcomeLabel = exit?.action === "sell" ? `${exit.pnl > 0 ? "Won" : exit.pnl < 0 ? "Lost" : "Flat"} · ${result}` : result;
    return `<tr class="${outcomeClass}">
      <td class="title">${esc(row.title)}</td>
      <td>${esc((row.side || exit?.side || "—").toUpperCase())}</td>
      <td class="num">${esc(paid)}</td>
      <td class="trade-result" title="${esc(exit?.reason || "")}">${esc(outcomeLabel)}</td>
      <td class="num ${exit?.pnl < 0 ? "bad" : exit?.pnl > 0 ? "good" : ""}">${exit ? esc(`${exit.pnl > 0 ? "+" : exit.pnl < 0 ? "−" : ""}${cents(Math.abs(exit.pnl))}`) : "—"}</td>
      <td class="num" title="${esc(entry ? new Date(entry.ts).toLocaleString() : "")}">${entry ? esc(time(entry.ts)) : "—"}</td>
      <td class="num" title="${esc(exit ? new Date(exit.ts).toLocaleString() : "")}">${exit ? esc(time(exit.ts)) : "—"}</td>
    </tr>`;
  }).join("");
}

function drawProfit(points) {
  const chart = $("equity");
  const series = [0, ...points.map(p => p.cumulative_pnl)];
  const total = series[series.length - 1];
  $("profit-total").textContent = money(total, 3);
  $("profit-total").className = `num ${total < 0 ? "bad" : total > 0 ? "good" : ""}`;
  $("profit-empty").classList.toggle("hidden", points.length > 0);
  chart.classList.toggle("hidden", points.length === 0);
  const lo = Math.min(...series), hi = Math.max(...series);
  const pad = Math.max((hi - lo) * 0.2, 0.01);
  const min = lo - pad, max = hi + pad;
  const x = i => 95 + i * 970 / Math.max(1, points.length);
  const y = value => 20 + (max - value) / (max - min) * 175;
  const ticks = [...new Set([lo, 0, hi])];
  let path = `M ${x(0)} ${y(0)}`;
  series.slice(1).forEach((value, i) => { path += ` H ${x(i + 1)} V ${y(value)}`; });
  chart.innerHTML = `<title>Cumulative net profit from ${points.length} completed trades</title>
    ${ticks.map(value => `<line x1="95" x2="1065" y1="${y(value)}" y2="${y(value)}" stroke="${value === 0 ? '#948773' : '#38332b'}" stroke-dasharray="4 5"/><text x="82" y="${y(value) + 4}" text-anchor="end" fill="#afa595" font-size="13">${esc(money(value, 3))}</text>`).join("")}
    <path d="${path}" fill="none" stroke="#e0b56a" stroke-width="2.5"/>
    <text x="95" y="225" fill="#afa595" font-size="13">Start · $0</text>
    <text x="1065" y="225" text-anchor="end" fill="#afa595" font-size="13">${points.length} completed trade${points.length === 1 ? '' : 's'}</text>
    ${points.map((point, i) => {
      const label = `${point.title} · ${point.side ? point.side.toUpperCase() : 'Pick unavailable'} · ${new Date(point.ts).toLocaleString()} · Trade ${money(point.pnl, 3)} · Total ${money(point.cumulative_pnl, 3)}`;
      return `<circle cx="${x(i + 1)}" cy="${y(point.cumulative_pnl)}" r="6" fill="${point.pnl < 0 ? '#df967e' : '#a9ce8b'}" tabindex="0" aria-label="${esc(label)}"><title>${esc(label)}</title></circle>`;
    }).join("")}`;
}

async function pull() {
  const response = await fetch("/api/state");
  render(await response.json());
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  socket.onmessage = (event) => render(JSON.parse(event.data));
  socket.onclose = () => {
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
  if (!confirm("Reset the paper book to the starting cash? The tape, the open trades, and the audit will be cleared. The settled record is kept.")) return;
  await post("/api/reset");
});

document.body.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  if (target.dataset.block) {
    await post("/api/block", { key: target.dataset.block });
  } else if (target.dataset.close) {
    if (!confirm("Close this paper clip at the current bid, if the book can fill it?")) return;
    await post("/api/close", { id: target.dataset.close });
  } else if (target.dataset.tighten) {
    await post("/api/knob", { key: target.dataset.tighten });
  }
});

setInterval(() => {
  if (!state) return;
  $("next").textContent = state.status === "scanning" ? "Reading the books" : `Next scan ${countdown(state.next_scan_at)}`;
}, 1000);

pull().catch(() => {});
connect();
