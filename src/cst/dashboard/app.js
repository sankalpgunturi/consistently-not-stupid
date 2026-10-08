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
let pendingCommand = null;
let tradeLimit = 5;
const expandedTrades = new Set();
const knobDrafts = new Map();
let knobBusy = false;
let knobRenderStamp = "";
let liveAwaiting = false;

function render(next) {
  state = next;
  if (pendingCommand && next.last_command?.id === pendingCommand) {
    pendingCommand = null;
    lastStamp = "";
    if (liveAwaiting) {
      liveAwaiting = false;
      const approve = $("live-approve");
      if (approve) approve.disabled = false;
      if (!next.last_command.ok) showLiveError(next.last_command.error || "The desk refused that change.");
      else $("live-dialog").close();
    } else if (!next.last_command.ok) window.alert(next.last_command.error || "The command failed.");
  }
  const book = next.book || {};
  $("next").textContent = next.status === "scanning" ? "Reading the books" : `Next scan ${countdown(next.next_scan_at)}`;
  $("pause").textContent = next.operator_pause ? "Resume buys" : "Pause buys";
  const liveButton = $("live");
  if (next.mode === "live") {
    liveButton.disabled = true;
    liveButton.textContent = `Live · ${money(next.live_budget || 0, 0)}`;
    liveButton.title = "Live trading is on for this amount.";
  } else {
    liveButton.disabled = false;
    liveButton.textContent = "Switch to live trading";
    liveButton.title = "Place real Kalshi orders up to an amount you approve.";
  }
  const modeNote = $("mode-note");
  if (modeNote) {
    modeNote.textContent = next.mode === "live"
      ? `Kalshi live trading · ${money(next.live_budget || 0, 0)} approved`
      : "Kalshi paper trading";
  }
  const stamp = JSON.stringify({
    status: next.status,
    operator_pause: next.operator_pause,
    mode: next.mode,
    live_budget: next.live_budget,
    live_exchange_balance: next.live_exchange_balance,
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
    trade_reviews: next.trade_reviews,
    sale_reviews: next.sale_reviews,
    market_links: next.market_links,
    realized_curve: next.realized_curve,
    focus: next.focus,
    retro: next.latest_model_review,
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

  const old = document.querySelector(".banner");
  if (old) old.remove();
  const messages = [];
  if (book.drawdown_pause) messages.push("New buys are paused. The book is under its peak by the pause line. Exits still run.");
  if (next.operator_pause) messages.push("New buys are paused by you. Exits still run.");
  if (next.mode === "live" && Number.isFinite(Number(next.live_exchange_balance))
      && Number(next.live_exchange_balance) + 1e-9 < Number(next.live_budget)) {
    messages.push(`Kalshi balance is ${money(next.live_exchange_balance)}. Buys stop at the smaller of that and the ${money(next.live_budget, 0)} you approved.`);
  }
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
  $("run-summary").textContent = next.status === "stale" ? "Updates paused" : next.status === "error" ? "Scan failed" :
    book.entries_paused ? "Paused" : next.status === "scanning" ? "Scanning" : next.mode === "live" ? "Live" : "Running";

  drawProfit(next.realized_curve || []);
  renderKnobs(next.params || []);
  renderTrades(next.trades || [], next.positions || []);
}

function renderKnobs(rows) {
  // Do not replace focused/dragged controls when only market data changes.
  const stamp = JSON.stringify({rows, disabled: knobBusy || Boolean(pendingCommand)});
  if (stamp === knobRenderStamp) return;
  knobRenderStamp = stamp;
  $("knobs").innerHTML = rows.filter(row => row.key !== "amount_per_bet" || !rows.some(r => r.key === "all_in" && r.value)).map(row => {
    let draft = knobDrafts.get(row.key);
    if (draft && draft.current !== row.value) { knobDrafts.delete(row.key); draft = null; }
    const value = draft?.value ?? row.value;
    const adjustable = row.adjustable !== false && Number.isFinite(row.step);
    const changed = value !== row.value;
    return `<div class="knob">
      <div class="knob-setting"><label for="knob-${esc(row.key)}">${esc(row.label)}</label>
      <output aria-live="polite" id="value-${esc(row.key)}">${esc(formatKnob({...row, value}))}</output>
      ${adjustable ? `<input type="range" id="knob-${esc(row.key)}" data-knob="${esc(row.key)}" min="0" max="${Math.round((row.max-row.min)/row.step)}" step="1" value="${Math.round((value-row.min)/row.step)}" aria-valuetext="${esc(formatKnob({...row,value}))}" title="Choose a value, then apply" ${knobBusy || pendingCommand ? 'disabled' : ''}>
      <div class="slider-bounds" aria-hidden="true"><span>${esc(formatKnob({...row,value:row.min}))}</span><span>${esc(formatKnob({...row,value:row.max}))}</span></div>` : ''}</div>
      ${adjustable ? `<button class="mini" data-tighten="${esc(row.key)}" ${!changed || knobBusy || pendingCommand ? 'disabled' : ''}>Apply</button>` : ''}
    </div>`;
  }).join("");
}

$("knobs").addEventListener("input", event => {
  const input = event.target;
  const row = state?.params?.find(row => row.key === input.dataset.knob);
  if (!row) return;
  const value = Number((row.min + Number(input.value) * row.step).toFixed(8));
  knobDrafts.set(row.key, {current: row.value, value});
  const label = formatKnob({...row, value});
  $("value-" + row.key).textContent = label;
  input.setAttribute("aria-valuetext", label);
  input.closest(".knob").querySelector("button").disabled = value === row.value;
});

function formatKnob(row) {
  const value = Number(row.value);
  if (row.key === "all_in") return value ? "All in" : "Fixed amount";
  if (row.key === "pick_underdog") return value ? "Underdog" : "Favorite";
  const cents = new Set(["min_edge", "min_win_profit", "max_spread", "stop_gap"]);
  const percent = new Set([
    "min_probability", "max_position_fraction", "max_deployed_fraction",
    "max_category_fraction", "max_drawdown", "correlation_threshold",
  ]);
  if (row.key === "stop_loss_minutes") return value === 0 ? "Off" : `Last ${value} min`;
  if (row.key === "exit_probability") return `${Math.round(value * 100)}%`;
  if (row.key === "amount_per_bet") return `$${value.toFixed(2)}`;
  if (cents.has(row.key)) return `${(value * 100).toFixed(1)}¢`;
  if (percent.has(row.key)) return `${(value * 100).toFixed(1)}%`;
  if (row.key === "entry_window_minutes") return `${value} min`;
  if (row.key === "scan_interval_seconds") return value < 60 ? `${value} sec` : `${Math.floor(value / 60)} min${value % 60 ? ` ${value % 60} sec` : ""}`;
  if (row.key === "min_hours_to_expiry") return `${value} hours`;
  if (row.key === "max_days_to_expiry") return `${value} days`;
  return String(row.value);
}

function renderTrades(rows, positions = []) {
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
  // Positions are authoritative, including entries older than the recent tape.
  const openStories = positions.map(position => ({entry: {
    ...(pending.get(`${position.venue}:${position.market_id}`)?.entry || {}),
    venue: position.venue, market_id: position.market_id, title: position.title,
    side: position.side, shares: position.shares, price: position.entry_price,
    fee: position.cost_basis - position.shares * position.entry_price,
    ts: position.opened_at, reason: position.reason,
  }, exit: null, position}));
  const all = [...openStories, ...stories.filter(story => story.exit).reverse()];
  const visible = all.slice(0, tradeLimit);
  const more = $("more-trades");
  const remaining = Math.min(5, all.length - visible.length);
  more.classList.toggle("hidden", remaining === 0);
  more.textContent = `Show ${remaining} more`;
  more.setAttribute("aria-label", `Show ${remaining} more trades`);
  const time = (ts) => new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  const cents = (n) => `${Number((n * 100).toFixed(3))}¢`;
  $("trades-empty").classList.toggle("hidden", visible.length > 0);
  $("trades").innerHTML = visible.map(({entry, exit, position}) => {
    const row = entry || exit;
    const stopped = exit?.action === "sell" && /stop.loss|bid fell/i.test(exit.reason || "");
    const paid = entry ? cents(entry.shares * entry.price + entry.fee) : "—";
    const sale = exit?.action === "sell" ? state?.sale_reviews?.[exit.id] : null;
    const sold = exit?.action === "sell";
    const outcomeClass = sold ? (sale?.verdict === "good" ? "trade-win" : sale?.verdict === "bad" ? "trade-loss" : "")
      : !exit ? "" : exit.pnl > 0 ? "trade-win" : exit.pnl < 0 ? "trade-loss" : "";
    const outcomeLabel = !exit ? "Open" : sold
      ? (sale?.verdict === "good" ? "Sold ✓" : sale?.verdict === "bad" ? "Sold ✕" : "Sold …")
      : exit.won ? "Won" : "Lost";
    const statusMeaning = sold ? (sale?.verdict === "good" ? "Good sell: our pick ultimately lost"
      : sale?.verdict === "bad" ? "Bad sell: our pick ultimately won" : "Sold: awaiting official result") : outcomeLabel;
    const probability = entry ? `${Number((entry.price * 100).toFixed(2))}%` : "—";
    const closeAt = exit?.ts || position?.end_time;
    const closeLabel = exit ? "Closed" : "Expected close";
    const profitIfWin = position?.profit_if_win;
    const profit = exit ? exit.pnl : profitIfWin;
    const tradeId = entry?.id || position?.id || exit.id;
    const expanded = expandedTrades.has(tradeId);
    const detailId = `detail-${tradeId}`;
    const rawUrl = state?.market_links?.[`${row.venue}:${row.market_id}:${row.side}`] || position?.url;
    let marketUrl = '';
    try { const url = new URL(rawUrl); if (url.protocol === 'https:' && (url.hostname === 'kalshi.com' || url.hostname.endsWith('.kalshi.com'))) marketUrl = url.href; } catch {}
    const pick = String(row.side || exit?.side || '').toUpperCase();
    const winProfit = entry ? entry.shares - (entry.shares * entry.price + entry.fee) : null;
    const paidValue = entry ? entry.shares * entry.price + entry.fee : position?.cost_basis;
    const returnValue = exit ? Math.max(0, exit.shares * exit.price - exit.fee) : null;
    const entryStory = entry
      ? `Bought at ${time(entry.ts)} when our pick was priced at ${probability}. Paid ${paid} including fees.`
      : 'The original purchase details are unavailable.';
    const why = Number.isFinite(winProfit) && winProfit > 0
      ? `We entered ${entry?.signal === "paper_underdog" ? "against the favorite" : "for the high quoted probability"}, with ${cents(winProfit)} profit if our pick won.` : '';
    let outcomeStory;
    if (!exit) {
      outcomeStory = `Waiting for the official result.${closeAt ? ` Market closes at ${time(closeAt)}; settlement may follow later.` : ''} Win: +${cents(profitIfWin || 0)}. Lose: −${cents(paidValue || 0)}.`;
    } else if (exit.action === 'settle') {
      const winner = exit.won ? pick : pick === 'YES' ? 'NO' : 'YES';
      outcomeStory = `Kalshi settled ${winner}. Our pick ${exit.won ? 'won' : 'lost'}. Received ${cents(returnValue)} at ${time(exit.ts)}. ${exit.pnl >= 0 ? 'Profit' : 'Loss'}: ${cents(Math.abs(exit.pnl))} after fees.`;
    } else {
      outcomeStory = `${stopped ? 'Stop loss closed this bet' : 'We closed this bet'} before settlement at ${time(exit.ts)}. Received ${cents(returnValue)}. ${exit.pnl >= 0 ? 'Profit' : 'Loss'}: ${cents(Math.abs(exit.pnl))} after fees.${stopped ? ` ${exit.reason}` : ''}`;
      outcomeStory += sale?.verdict === 'good'
        ? ` Good sell: Kalshi settled ${sale.result.toUpperCase()}. Our pick would have lost. Selling recovered ${cents(sale.sale_return)} instead of $0.`
        : sale?.verdict === 'bad'
          ? ` Bad sell: Kalshi settled ${sale.result.toUpperCase()}. Our pick would have won. Holding would have returned ${cents(sale.hold_return)}, ${cents(-sale.advantage)} more than selling.`
          : ' Waiting for the official result to see whether selling helped.';
    }
    const detailHtml = `<div class="trade-detail-body trade-story">
      <p class="trade-story-pick">We picked <strong>${esc(pick || 'an unrecorded side')}</strong>.</p>
      <p>${esc(entryStory)}</p>
      ${why ? `<p>${esc(why)}</p>` : ''}
      <p class="${exit ? exit.pnl < 0 ? 'bad' : 'good' : ''}">${esc(outcomeStory)}</p>
    </div>`;
    return `<tr class="trade-row ${outcomeClass}" data-trade="${esc(tradeId)}" tabindex="0" aria-expanded="${expanded}" aria-controls="${esc(detailId)}" aria-label="${esc(row.title)}: trade details">
      <td class="title" data-label="Trade">${marketUrl ? `<a class="trade-link" href="${esc(marketUrl)}" target="_blank" rel="noopener noreferrer">${esc(row.title)}</a>` : esc(row.title)}</td>
      <td data-label="Pick">${esc((row.side || exit?.side || "—").toUpperCase())}</td>
      <td class="num" data-label="Entry probability" title="Market-implied probability from our entry price, before fees">${esc(probability)}</td>
      <td class="num" data-label="Paid">${esc(paid)}</td>
      <td class="trade-result" data-label="Status" aria-label="${esc(statusMeaning)}" title="${esc(statusMeaning)}">${esc(outcomeLabel)}</td>
      <td data-label="Profit" class="num ${exit && profit < 0 ? "bad" : exit && profit > 0 ? "good" : ""}" title="${exit ? 'Realized profit after fees' : 'Profit after fees if the bet wins; not probability-weighted'}">${Number.isFinite(profit) ? esc(`${profit > 0 ? "+" : profit < 0 ? "−" : ""}${cents(Math.abs(profit))}`) : "—"}${!exit ? '<sup class="expected-mark" aria-label="expected if won">*</sup>' : ''}</td>
      <td class="num" data-label="Opened" title="${esc(entry ? new Date(entry.ts).toLocaleString() : "")}">${entry ? esc(time(entry.ts)) : "—"}</td>
      <td class="num" data-label="${closeLabel}" title="${esc(closeAt ? `${closeLabel}: ${new Date(closeAt).toLocaleString()}${exit ? '' : '; official settlement may follow later'}` : '')}">${closeAt ? esc(time(closeAt)) : "—"}${!exit && closeAt ? '<sup class="expected-mark" aria-label="expected close">*</sup>' : ''}</td>
    </tr><tr class="trade-expansion"><td colspan="8"><div id="${esc(detailId)}" class="trade-reveal ${expanded ? 'is-open' : ''}" ${expanded ? '' : 'inert'}><div class="trade-reveal-clip">${detailHtml}</div></div></td></tr>`;
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
  if (response.status === 202) { pendingCommand = payload?.id || null; return payload; }
  if (!response.ok) {
    const message = payload?.error || "The desk refused that change.";
    window.alert(message);
    if (payload?.state) render(payload.state);
    return null;
  }
  if (payload) render(payload);
  return payload;
}

function toggleTrade(row) {
  const id = row.dataset.trade;
  const expanded = !expandedTrades.has(id);
  if (expanded) expandedTrades.add(id); else expandedTrades.delete(id);
  row.setAttribute("aria-expanded", String(expanded));
  const detail = document.getElementById(row.getAttribute("aria-controls"));
  detail.inert = !expanded;
  detail.classList.toggle("is-open", expanded);
}
$("trades").addEventListener("click", event => {
  if (event.target.closest("a")) return;
  const row = event.target.closest(".trade-row");
  if (row) toggleTrade(row);
});
$("trades").addEventListener("keydown", event => {
  if (event.target.closest("a")) return;
  if (event.key !== "Enter" && event.key !== " ") return;
  const row = event.target.closest(".trade-row");
  if (row) { event.preventDefault(); toggleTrade(row); }
});

$("more-trades").addEventListener("click", () => {
  tradeLimit += 5;
  renderTrades(state?.trades || [], state?.positions || []);
});

$("pause").addEventListener("click", () => {
  post("/api/pause", { paused: !state?.operator_pause });
});

function showLiveError(message) {
  const error = $("live-error");
  if (error) error.textContent = message || "";
}

function liveAmount() {
  const raw = $("live-amount").value.trim();
  if (!/^\d+$/.test(raw)) return null;
  const amount = Number(raw);
  if (amount < 1 || amount > 5000) return null;
  return amount;
}

$("live").addEventListener("click", () => {
  if (state?.mode === "live") return;
  $("live-amount").value = "1";
  showLiveError("");
  $("live-dialog").showModal();
  $("live-amount").focus();
  $("live-amount").select();
});

$("live-cancel").addEventListener("click", () => {
  $("live-dialog").close();
});

$("live-amount").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    $("live-approve").click();
  }
});

$("live-approve").addEventListener("click", async () => {
  const amount = liveAmount();
  if (amount === null) {
    showLiveError("Enter a whole dollar amount from $1 to $5,000.");
    return;
  }
  const approve = $("live-approve");
  approve.disabled = true;
  showLiveError("");
  try {
    const response = await fetch("/api/live", {
      method: "POST",
      headers: { "X-CSRF-Token": state?.csrf || "", "Content-Type": "application/json" },
      body: JSON.stringify({ amount }),
    });
    let payload = null;
    try { payload = await response.json(); } catch (_err) { payload = null; }
    if (response.status === 202) {
      pendingCommand = payload?.id || null;
      liveAwaiting = true;
      showLiveError("Waiting for the desk.");
      return;
    }
    if (!response.ok) {
      showLiveError(payload?.error || "The desk refused that change.");
      if (payload?.state) render(payload.state);
      return;
    }
    if (payload) render(payload);
    $("live-dialog").close();
  } finally {
    if (!liveAwaiting) approve.disabled = false;
  }
});

document.body.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  if (target.dataset.block) {
    await post("/api/block", { key: target.dataset.block });

  } else if (target.dataset.tighten) {
    const key = target.dataset.tighten;
    const draft = knobDrafts.get(key);
    if (knobBusy || pendingCommand || !draft || draft.value === draft.current) return;
    knobBusy = true;
    renderKnobs(state?.params || []);
    try { await post("/api/knob", { key, value: draft.value }); }
    finally { knobBusy = false; knobDrafts.delete(key); renderKnobs(state?.params || []); }
  }
});

setInterval(() => {
  if (!state) return;
  if (window.CST_REMOTE) {
    $("next").textContent = state.published_at ? `Updated ${new Date(state.published_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}` : "";
    if (!state.published_at || Date.now() - Date.parse(state.published_at) > 30000) $("run-summary").textContent = "Updates paused";
  } else {
    $("next").textContent = state.status === "scanning" ? "Reading the books" : `Next scan ${countdown(state.next_scan_at)}`;
  }
}, 1000);

pull().catch(() => {});
if (window.CST_REMOTE) {
  let polling = false;
  setInterval(async () => {
    if (polling || document.hidden) return;
    polling = true;
    try { await pull(); }
    catch { $("run-summary").textContent = "Updates paused"; }
    finally { polling = false; }
  }, 5000);
} else {
  connect();
}
