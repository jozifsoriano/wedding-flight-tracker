let data = null;
let sparks = {};
let chart = null;

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => n == null ? "—" : new Intl.NumberFormat("en-US", {
  style: "currency", currency: data?.trip.currency || "USD", maximumFractionDigits: 0,
}).format(n);
const fmtDate = (iso) => iso ? new Date(iso + (iso.length === 10 ? "T12:00:00" : "")).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
const fmtWhen = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "never";
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// ---------- tabs ----------
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== `tab-${name}`));
  try { localStorage.setItem("tab", name); } catch {}
  if (name === "history") renderHistory();
  if (name === "searches") renderSearches();
}

// ---------- shared bits ----------
function levelTag(level) {
  if (!level) return "";
  return `<span class="level ${esc(level)}">${level === "low" ? "▼" : level === "high" ? "▲" : "●"} ${esc(level)} price</span>`;
}

function legsList(plan) {
  return `<ul class="legs">${plan.components.map((c) => {
    const offer = c.top_offer ? `<br><span class="muted">${esc(c.top_offer.airline)} · ${c.top_offer.stops === 0 ? "nonstop" : esc(c.top_offer.stops) + " stop"} · ${esc(c.top_offer.duration)}</span>` : "";
    const amt = c.price == null ? `<span class="missing">not priced yet</span>` : money(c.price);
    const link = c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${amt}</a>` : amt;
    return `<li><span class="who"><b>${esc(c.role)}</b> <span class="tag">${esc(c.cabin)}</span>${offer}</span><span class="amt">${link}</span></li>`;
  }).join("")}</ul>`;
}

function planDates(p) {
  const d = p.dates;
  if (p.type === "wedding") return `Leave ${fmtDate(d.outbound)} · home ${fmtDate(d.home)}`;
  return `Leave ${fmtDate(d.outbound)} · honeymoon ${fmtDate(d.honeymoon_start)}–${fmtDate(d.honeymoon_end)}` + (d.home ? ` · home ${fmtDate(d.home)}` : "");
}

function comparisonNote(plan) {
  if (!["open_jaw", "multicity"].includes(plan.type) ||
      !data.destinations.find((d) => d.id === plan.destination_id)?.extended_stay) return "";
  const sameItinerary = (p) => p.destination_id === plan.destination_id && p.gateway === plan.gateway &&
    p.dates.outbound === plan.dates.outbound && p.dates.honeymoon_start === plan.dates.honeymoon_start &&
    p.dates.honeymoon_end === plan.dates.honeymoon_end && p.airport === plan.airport;
  const otherType = plan.type === "multicity" ? "open_jaw" : "multicity";
  const other = data.plans.find((p) => p.type === otherType && sameItinerary(p));
  if (!other) return plan.type === "open_jaw" && !data.status.serpapi_enabled
    ? "Multi-city ticket comparison requires SerpApi; this total includes the separate DAD hop."
    : "";
  if (plan.total == null || other.total == null) return "Comparison needs prices for both ticket options.";
  const delta = Math.abs(plan.total - other.total);
  if (delta === 0) return "Multi-city and separate long-haul tickets are currently the same price.";
  const cheaper = plan.type === "multicity" ? plan.total < other.total : other.total < plan.total;
  return `${cheaper ? "Multi-city saves" : "Separate long-haul tickets save"} ${money(delta)}; both totals include the separate DAD hop.`;
}

function sparkline(points) {
  if (!points || points.length < 2) return `<p class="muted small">Trend appears after a few refreshes.</p>`;
  const w = 300, h = 40, pad = 5;
  const vals = points.map((p) => p.total);
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const xy = vals.map((v, i) => [pad + (i * (w - 2 * pad)) / (vals.length - 1), pad + (1 - (v - min) / span) * (h - 2 * pad)]);
  const d = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  const [lx, ly] = xy[xy.length - 1];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Price trend from ${money(vals[0])} to ${money(vals[vals.length - 1])}">
    <title>Low ${money(min)} · high ${money(max)} · now ${money(vals[vals.length - 1])}</title>
    <path d="${d}" vector-effect="non-scaling-stroke"/><circle cx="${lx}" cy="${ly}" r="4"/></svg>`;
}

// ---------- best trips ----------
function renderBest() {
  const trips = data.dashboard_trips;
  const best = data.best_by_destination;
  const top = best.find((p) => p.type !== "wedding");
  const wedding = best.find((p) => p.type === "wedding");
  $("#hero").innerHTML = top ? `
    <div><div class="label">Cheapest wedding + honeymoon</div><div class="big">${money(top.total)}</div>
      <div class="meta">${money(top.per_person)} per person · for ${data.trip.adults}</div></div>
    <div><div class="label">Honeymoon</div><h2>${esc(top.destination)}</h2><div class="meta">${esc(top.type_label)} · via ${esc(top.gateway)}</div></div>
    ${wedding ? `<div><div class="label">Wedding flights alone</div><div class="price">${money(wedding.total)}</div>
    <div class="meta">so the honeymoon adds ${money(top.total - wedding.total)}</div></div>` : ""}
    ${comparisonNote(top) ? `<div class="meta">${esc(comparisonNote(top))}</div>` : ""}`
    : `<div><div class="label">No complete prices yet</div><p class="meta">The first refresh takes a few minutes (each search waits a bit to stay polite to Google). Check the Searches tab for progress.</p></div>`;

  $("#best-grid").innerHTML = trips.map((p) => `
    <article class="card">
      <div class="card-head">
        <div><h3>${esc(p.destination)}</h3><div class="meta">${p.total == null ? "Tracking · awaiting prices" : `${esc(p.type_label)} · via ${esc(p.gateway)}${p.airport ? " · " + esc(p.airport) : ""}`}</div></div>
        <div style="text-align:right"><div class="price">${money(p.total)}</div><div class="meta">${p.total == null ? "estimate pending" : `${money(p.per_person)} pp`}</div></div>
      </div>
      ${p.total != null ? `<div class="meta">${planDates(p)}</div>` : ""}
      ${comparisonNote(p) ? `<div class="meta">${esc(comparisonNote(p))}</div>` : ""}
      ${sparkline(sparks[p.destination_id])}
      ${legsList(p)}
    </article>`).join("") || "";
}

// ---------- all plans ----------
function fillSelect(sel, entries, keep) {
  const prev = sel.value;
  sel.innerHTML = (keep ? `<option value="">All</option>` : "") + entries.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

function renderPlans() {
  const dests = [["wedding-only", "Wedding only"], ...data.destinations.map((d) => [d.id, `${d.name}, ${d.country}`])];
  fillSelect($("#f-dest"), dests, true);
  fillSelect($("#f-type"), Object.entries(data.plan_types), true);
  fillSelect($("#f-gw"), data.trip.gateways.map((g) => [g, g]), true);
  const fd = $("#f-dest").value, ft = $("#f-type").value, fg = $("#f-gw").value, fc = $("#f-complete").checked;
  const rows = data.plans.filter((p) => (!fd || p.destination_id === fd) && (!ft || p.type === ft) && (!fg || p.gateway === fg) && (!fc || p.total != null));
  $("#plans-table").innerHTML = `
    <thead><tr><th>Destination</th><th>Plan</th><th>Gateway</th><th>Dates</th><th class="num">Total for ${data.trip.adults}</th><th class="num">Per person</th><th>Breakdown</th></tr></thead>
    <tbody>${rows.slice(0, 400).map((p) => `<tr>
      <td>${esc(p.destination)}${p.airport ? ` <span class="tag">${esc(p.airport)}</span>` : ""}</td>
      <td>${esc(p.type_label)}${comparisonNote(p) ? `<div class="muted small">${esc(comparisonNote(p))}</div>` : ""}</td>
      <td>${esc(p.gateway)}</td><td>${planDates(p)}</td>
      <td class="num"><b>${money(p.total)}</b></td><td class="num">${money(p.per_person)}</td>
      <td><details><summary>${p.components.length} flights</summary>${legsList(p)}</details></td>
    </tr>`).join("") || `<tr><td colspan="7" class="muted">No plans match.</td></tr>`}</tbody>`;
}
["#f-dest", "#f-type", "#f-gw", "#f-complete"].forEach((s) => $(s).addEventListener("change", renderPlans));

// ---------- history ----------
const SERIES = { rt_side: "--series-1", open_jaw: "--series-2", multicity: "--series-3", wedding: "--series-1" };

async function renderHistory() {
  const sel = $("#h-dest");
  if (!sel.options.length) {
    fillSelect(sel, [...data.destinations.map((d) => [d.id, `${d.name}, ${d.country}`]), ["wedding-only", "Wedding only"]], false);
    try { const saved = localStorage.getItem("h-dest"); if (saved) sel.value = saved; } catch {}
  }
  const id = sel.value;
  $("#h-title").textContent = sel.options[sel.selectedIndex]?.text || "";
  const series = await api(`/api/history?destination_id=${encodeURIComponent(id)}`);
  const types = Object.keys(series);
  $("#h-empty").hidden = types.length > 0;
  const grid = cssVar("--grid"), ink = cssVar("--text-2");
  // Pad the time axis so one or two refreshes don't produce a bogus range.
  const xs = types.flatMap((t) => series[t].map((p) => new Date(p.at).getTime()));
  const DAY = 864e5, lo = Math.min(...xs), hi = Math.max(...xs);
  const pad = hi - lo < DAY ? DAY / 2 : 0;
  if (chart) chart.destroy();
  chart = new Chart($("#h-chart"), {
    type: "line",
    data: {
      datasets: types.map((t) => ({
        label: data.plan_types[t],
        data: series[t].map((p) => ({ x: new Date(p.at).getTime(), y: p.total })),
        borderColor: cssVar(SERIES[t]), backgroundColor: cssVar(SERIES[t]),
        borderWidth: 2, pointRadius: series[t].length > 30 ? 0 : 3, pointHoverRadius: 5, tension: 0,
      })),
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: {
        x: {
          type: "linear", min: xs.length ? lo - pad : undefined, max: xs.length ? hi + pad : undefined,
          grid: { color: grid },
          ticks: { color: ink, maxTicksLimit: 8, callback: (v) => new Date(v).toLocaleString(undefined, pad ? { hour: "numeric" } : { month: "short", day: "numeric" }) },
        },
        y: { grid: { color: grid }, ticks: { color: ink, callback: (v) => money(v) } },
      },
      plugins: {
        legend: { display: types.length > 1, labels: { color: ink, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: { callbacks: { title: (items) => fmtWhen(new Date(items[0].parsed.x).toISOString()), label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } },
      },
    },
  });
}
$("#h-dest").addEventListener("change", () => { try { localStorage.setItem("h-dest", $("#h-dest").value); } catch {} renderHistory(); });

// ---------- honeymoon ideas ----------
function renderIdeas() {
  const best = Object.fromEntries(data.best_by_destination.map((p) => [p.destination_id, p]));
  $("#ideas-grid").innerHTML = data.destinations.map((d) => {
    const p = best[d.id];
    return `<article class="card">
      <div class="card-head">
        <div><h3>${esc(d.name)}</h3><div class="meta">${esc(d.country)} · ${d.airports.map(esc).join(", ")}</div></div>
        <div style="text-align:right"><div class="price">${money(p?.total)}</div><div class="meta">${p ? "full trip for " + data.trip.adults : "not priced yet"}</div></div>
      </div>
      <dl>
        <dt>March</dt><dd>${esc(d.march)}</dd>
        <dt>From DAD</dt><dd>${esc(d.from_dad)}</dd>
        <dt>Vibe</dt><dd>${esc(d.vibe)}</dd>
        <dt>Entry</dt><dd>${esc(d.entry)}</dd>
      </dl>
      ${d.tip ? `<div class="tip">💡 ${esc(d.tip)}</div>` : ""}
    </article>`;
  }).join("");
}

function renderAddCity() {
  if (!$("#city-start-date").value) $("#city-start-date").value = data.trip.dates.honeymoon_start[0] || "";
  if (!$("#city-end-date").value) $("#city-end-date").value = data.trip.dates.honeymoon_end[0] || "";
}

$("#add-city-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#add-city-btn"), message = $("#add-city-msg");
  button.disabled = true;
  message.textContent = "Saving…";
  const payload = {
    name: $("#city-name").value,
    country: $("#city-country").value,
    airports: $("#city-airports").value.split(/[,\s]+/).filter(Boolean),
    start_date: $("#city-start-date").value,
    end_date: $("#city-end-date").value,
    extended_stay: $("#city-extended").checked,
  };
  try {
    const result = await api("/api/destinations", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    message.textContent = `Added — ${result.searches} searches will be tracked. Use Refresh prices for estimates now.`;
    $("#city-name").value = "";
    $("#city-country").value = "";
    $("#city-airports").value = "";
    await load();
  } catch (e) {
    message.textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

// ---------- searches / status ----------
async function renderSearches() {
  const s = data.status;
  $("#status-panel").innerHTML = `
    <div class="status-grid">
      <div><span class="muted small">Searches tracked</span><b>${s.search_count}</b></div>
      <div><span class="muted small">Last refresh</span><b>${fmtWhen(s.last_run?.started_at)}</b></div>
      <div><span class="muted small">Last result</span><b>${s.last_run ? `${s.last_run.ok} ok · ${s.last_run.errors} failed` : "—"}</b></div>
      <div><span class="muted small">Next refresh</span><b>${fmtWhen(s.next_run)}</b></div>
      <div><span class="muted small">SerpApi</span><b>${s.serpapi_enabled ? `${s.serpapi_used_this_month} / ${s.serpapi_budget} this month` : "off"}</b></div>
    </div>
    <button id="reload-btn" class="btn ghost">Reload config.yaml</button> <span id="reload-msg" class="muted small"></span>`;
  $("#reload-btn").onclick = async () => {
    try { const r = await api("/api/reload", { method: "POST" }); $("#reload-msg").textContent = `Reloaded — ${r.searches} searches.`; load(); }
    catch (e) { $("#reload-msg").textContent = e.message; }
  };
  const rows = await api("/api/searches");
  $("#searches-table").innerHTML = `
    <thead><tr><th>Search</th><th>Type</th><th>Cabin</th><th>Source</th><th class="num">Cheapest for ${data.trip.adults}</th><th>Level</th><th>Checked</th><th>Best option</th></tr></thead>
    <tbody>${rows.map((r) => {
      const l = r.last, o = l?.offers?.[0];
      return `<tr>
        <td>${esc(r.label)}</td><td>${esc(r.kind.replace("_", " "))}</td><td>${esc(r.cabin)}</td><td>${esc(r.provider)}</td>
        <td class="num">${l?.min_price ? `<a href="${esc(l.booking_url)}" target="_blank" rel="noopener">${money(l.min_price)}</a>` : "—"}</td>
        <td>${levelTag(l?.price_level)}</td><td>${fmtWhen(l?.fetched_at)}</td>
        <td>${o ? `${esc(o.airline)} · ${esc(o.depart)}` : l?.error ? `<span class="err">${esc(l.error)}</span>` : `<span class="muted">pending</span>`}</td>
      </tr>`;
    }).join("")}</tbody>`;
}

// ---------- refresh ----------
$("#run-btn").addEventListener("click", async () => {
  try { await api("/api/run", { method: "POST" }); } catch (e) { alert(e.message); }
  load();
});

function renderHeader() {
  const t = data.trip, s = data.status;
  $("#trip-summary").textContent = `${t.adults} travelers · ${t.home} → ${t.wedding_city} · ${t.cabins.long_haul} long-haul, ${t.cabins.regional} regional · leaving ${t.dates.outbound.map(fmtDate).join(" / ")}`;
  $("#run-btn").disabled = s.running;
  $("#run-status").textContent = s.running
    ? `Refreshing ${s.progress + 1}/${s.total}: ${s.current || ""}`
    : `Updated ${fmtWhen(s.last_run?.finished_at || s.last_run?.started_at)}`;
}

let timer = null;
async function load() {
  [data, sparks] = await Promise.all([api("/api/overview"), api("/api/sparklines")]);
  renderHeader();
  renderBest();
  renderPlans();
  renderIdeas();
  renderAddCity();
  const active = document.querySelector('.tabs [aria-selected="true"]').dataset.tab;
  if (active === "searches") renderSearches();
  if (active === "history") renderHistory();
  clearTimeout(timer);
  timer = setTimeout(load, data.status.running ? 8000 : 120000);
}

let startTab = "best";
try { startTab = localStorage.getItem("tab") || "best"; } catch {}
load().then(() => showTab(startTab)).catch((e) => { $("#trip-summary").textContent = "Can't reach the server: " + e.message; });
