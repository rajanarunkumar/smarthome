"use strict";
// Insights tab: water use, sprinkler runs and weather together. Charts are plain SVG.
// Colors come from --viz-* tokens in style.css (validated for color-blind separation in light and dark).

const SVGNS = "http://www.w3.org/2000/svg";
let insightDays = 28;
let insightTable = false;
let insightView = "water";

function fmtNum(n, digits = 0) {
  if (n == null) return "–";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}
function compact(n) {
  if (n == null) return "–";
  return Math.abs(n) >= 10000 ? `${fmtNum(n / 1000, 1)}K` : fmtNum(n);
}
function shortDay(iso) {
  const d = new Date(iso + "T12:00");
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function niceMax(v) {
  if (!v || v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v);
}
function el(tag, attrs = {}, parent) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}
// Column with a 4px rounded top and a square base.
function columnPath(x, y, w, h) {
  const r = Math.min(4, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

const tip = (() => {
  const t = document.createElement("div");
  t.className = "viz-tip"; t.hidden = true; t.setAttribute("role", "status");
  document.body.appendChild(t);
  return t;
})();
function showTip(evt, title, rows) {
  tip.replaceChildren();
  const h = document.createElement("div"); h.className = "viz-tip-title"; h.textContent = title; tip.appendChild(h);
  for (const [value, label, color] of rows) {
    const r = document.createElement("div"); r.className = "viz-tip-row";
    const key = document.createElement("span"); key.className = "viz-key-line"; key.style.background = color;
    const v = document.createElement("b"); v.textContent = value;
    const l = document.createElement("span"); l.textContent = label;
    r.append(key, v, l); tip.appendChild(r);
  }
  tip.hidden = false;
  const box = evt.target.getBoundingClientRect ? evt.target.getBoundingClientRect() : { left: evt.clientX, top: evt.clientY, width: 0 };
  const x = evt.clientX ?? box.left + box.width / 2, y = evt.clientY ?? box.top;
  tip.style.left = `${Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, x - tip.offsetWidth / 2))}px`;
  tip.style.top = `${Math.max(8, y - tip.offsetHeight - 12)}px`;
}
function hideTip() { tip.hidden = true; }

// Daily columns: stacked (sprinkler on household) or single, plus an optional line on the same unit.
function columnChart(container, { days, stacks, line, unit, digits = 0, forecast = () => false, label, note }) {
  // Drawn at the card's real width, so 11px text stays 11px on a phone.
  const W = Math.max(300, Math.round(container.getBoundingClientRect().width - 32)), H = 220, L = 40, R = 8, T = 12, B = 26;
  const totals = days.map((d, i) => stacks.reduce((s, st) => s + (st.values[i] || 0), 0));
  const maxV = niceMax(Math.max(...totals, ...(line ? line.values.map((v) => v || 0) : [0])));
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "viz", role: "img", "aria-label": label });
  const y = (v) => T + (H - T - B) * (1 - v / maxV);
  for (let k = 0; k <= 4; k++) {
    const v = (maxV / 4) * k;
    el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "viz-grid" }, svg);
    const t = el("text", { x: L - 6, y: y(v) + 4, class: "viz-axis", "text-anchor": "end" }, svg);
    t.textContent = digits ? fmtNum(v, digits) : compact(v);
  }
  const band = (W - L - R) / days.length;
  const bw = Math.min(24, Math.max(3, band - 2));
  const step = Math.ceil(days.length / Math.max(3, Math.floor(W / 80)));
  days.forEach((day, i) => {
    const x = L + band * i + (band - bw) / 2;
    let base = 0;
    stacks.forEach((st, si) => {
      const v = st.values[i] || 0;
      if (v <= 0) return;
      const top = si === stacks.length - 1 || stacks.slice(si + 1).every((s2) => !(s2.values[i] > 0));
      const y0 = y(base), y1 = y(base + v);
      const h = Math.max(0, y0 - y1 - (base > 0 ? 2 : 0)); // 2px surface gap between segments
      el("path", { d: top ? columnPath(x, y1, bw, h) : `M${x},${y1}h${bw}v${h}h${-bw}Z`,
        fill: st.color, class: `viz-mark${forecast(day) ? " viz-forecast" : ""}` }, svg);
      base += v;
    });
    if (i % step === 0) {
      const t = el("text", { x: x + bw / 2, y: H - 8, class: "viz-axis", "text-anchor": "middle" }, svg);
      t.textContent = shortDay(day);
    }
    // Hit target: the whole day band, taller than the marks.
    const hit = el("rect", { x: L + band * i, y: T, width: band, height: H - T - B, class: "viz-hit", tabindex: 0 }, svg);
    const rows = stacks.map((st) => [st.values[i] == null ? "–" : `${fmtNum(st.values[i], digits)} ${unit}`, st.name, st.color]);
    if (line) rows.push([line.values[i] == null ? "–" : `${fmtNum(line.values[i], 2)} ${unit}`, line.name, line.color]);
    const title = `${shortDay(day)}${forecast(day) ? " (forecast)" : ""}${note ? note(day) : ""}`;
    hit.addEventListener("pointermove", (e) => showTip(e, title, rows));
    hit.addEventListener("focus", (e) => showTip(e, title, rows));
    hit.addEventListener("pointerleave", hideTip); hit.addEventListener("blur", hideTip);
  });
  if (line) {
    const pts = line.values.map((v, i) => (v == null ? null : [L + band * i + band / 2, y(v)]));
    let d = "";
    pts.forEach((p, i) => { if (p) d += `${d && pts[i - 1] ? "L" : "M"}${p[0]},${p[1]}`; });
    el("path", { d, class: "viz-line", stroke: line.color }, svg);
    const last = [...pts].reverse().find(Boolean);
    if (last) el("circle", { cx: last[0], cy: last[1], r: 4, fill: line.color, class: "viz-dot" }, svg);
  }
  svg.setAttribute("width", W); svg.setAttribute("height", H);
  container.appendChild(svg);
}

function legend(items) {
  return `<div class="viz-legend">${items.map(([name, color, kind]) =>
    `<span><i class="viz-swatch ${kind === "line" ? "line" : ""}" style="background:${color}"></i>${esc(name)}</span>`).join("")}</div>`;
}

const STATUS_ICON = { good: "✓", warning: "!", serious: "!!" };

function insightControls() {
  return `
    <div class="filters">
      <span class="segmented" role="tablist">
        <button class="${insightView === "water" ? "on" : ""}" data-view="water" role="tab">Water</button>
        <button class="${insightView === "energy" ? "on" : ""}" data-view="energy" role="tab">Electricity</button>
      </span>
      ${[14, 21, 28].map((n) => `<button class="pill ${n === insightDays ? "primary" : ""}" data-days="${n}" aria-label="Last ${n} days">${n / 7} wk</button>`).join("")}
    </div>
    <div class="filters">
      <button class="pill" id="ins-table">${insightTable ? "Show charts" : "Show table"}</button>
      <button class="pill" id="ins-backfill" title="Fetch the last 4 weeks of water, electricity, weather and sprinkler history">Fetch last 4 weeks</button>
    </div>`;
}

function bindInsightControls() {
  view.querySelectorAll("[data-days]").forEach((b) => b.addEventListener("click", () => { insightDays = Number(b.dataset.days); renderInsights(true); }));
  view.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => { insightView = b.dataset.view; renderInsights(true); }));
  $("#ins-table")?.addEventListener("click", () => { insightTable = !insightTable; renderInsights(false); });
  $("#ins-backfill")?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Fetching… (about a minute)";
    try {
      const r = await api("POST", "/api/backfill");
      const got = [r.water_days && `${r.water_days} days of water`, r.energy_slots && `${Math.round(r.energy_slots / 96)} days of electricity`,
        r.weather_days && "weather", r.sprinkler_runs_reconstructed != null && `${r.sprinkler_runs_reconstructed} past sprinkler runs`].filter(Boolean);
      toast(r.problems.length ? r.problems.join("; ") : `Got ${got.join(", ") || "nothing new"}`);
      await renderInsights(true);
    } catch (err) { toast(err.message); e.target.disabled = false; e.target.textContent = "Fetch last 4 weeks"; }
  });
}

async function renderInsights(fetch = true) {
  if (insightView === "energy") return renderEnergy(fetch);
  if (fetch) state.insights = await api("GET", `/api/insights?days=${insightDays}`);
  const ins = state.insights;
  const css = getComputedStyle(document.documentElement);
  const color = (n) => css.getPropertyValue(`--viz-${n}`).trim();
  const C = { other: color(1), sprinkler: color(2), rain: color(3), et: color(4) };
  const s = ins.summary;
  const past = ins.series.filter((r) => !r.forecast);
  const nothing = !ins.has_water && !ins.has_weather && !ins.has_runs;

  view.innerHTML = `
    ${ins.location ? "" : `<div class="card pad"><b>Set your location for weather</b>
      <p class="note">Rain and evaporation come from Open‑Meteo, free and with no account.</p>
      <div class="row key-row"><input type="text" id="loc-q" placeholder="City, State (e.g. McKinney, TX)">
      <button class="pill primary" id="loc-save">Save</button></div></div>`}
    ${insightControls()}
    ${nothing ? `<p class="empty">Nothing to show yet. Connect McKinney water and Rain Bird in Settings, set your location, then tap <b>Fetch last 4 weeks</b>.</p>` : `
    <div class="tiles">
      <div class="tile"><span class="tile-label">Water used</span><span class="tile-value">${compact(s.total_gal)}</span><span class="tile-sub">gallons, ${ins.days} days</span></div>
      <div class="tile"><span class="tile-label">Sprinklers</span><span class="tile-value">${s.sprinkler_share == null ? "–" : `${Math.round(s.sprinkler_share * 100)}%`}</span><span class="tile-sub">${compact(s.sprinkler_gal)} gal, ${fmtNum(s.minutes)} min</span></div>
      <div class="tile"><span class="tile-label">Rain</span><span class="tile-value">${fmtNum(s.rain_in, 2)}</span><span class="tile-sub">inches</span></div>
      <div class="tile"><span class="tile-label">Evaporation</span><span class="tile-value">${fmtNum(s.et0_in, 2)}</span><span class="tile-sub">inches (lawn loses about 70%)</span></div>
    </div>
    ${ins.recommendations.length ? `<div class="section-title"><span>Suggestions</span></div>
    <div class="card">${ins.recommendations.map((r) => `
      <div class="row rec"><span class="status status-${r.level}" aria-hidden="true">${STATUS_ICON[r.level]}</span>
        <div class="main"><span class="name">${esc(r.title)}</span><span class="sub wrap">${esc(r.text)}</span></div></div>`).join("")}</div>` : ""}
    <div id="ins-body"></div>`}`;

  $("#loc-save")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { const r = await api("PUT", "/api/location", { query: $("#loc-q").value.trim() }); toast(`Weather for ${r.name}`); renderInsights(true); }
    catch (err) { toast(err.message); e.target.disabled = false; }
  });
  bindInsightControls();
  const body = $("#ins-body");
  if (!body) return;

  if (insightTable) {
    body.innerHTML = `<div class="table-wrap"><table class="viz-table"><thead><tr>
      <th>Day</th><th>Water (gal)</th><th>Sprinklers (gal)</th><th>Everything else (gal)</th><th>Sprinkler min</th><th>Rain (in)</th><th>Evaporation (in)</th><th>High (°F)</th></tr></thead>
      <tbody>${ins.series.map((r) => `<tr><td>${esc(shortDay(r.day))}${r.forecast ? " (forecast)" : ""}</td><td>${fmtNum(r.total_gal)}</td><td>${fmtNum(r.sprinkler_gal)}</td><td>${fmtNum(r.other_gal)}</td><td>${fmtNum(r.minutes)}</td><td>${fmtNum(r.rain_in, 2)}</td><td>${fmtNum(r.et0_in, 2)}</td><td>${fmtNum(r.tmax_f)}</td></tr>`).join("")}</tbody></table></div>`;
  } else {
    body.innerHTML = `
      <div class="section-title"><span>Daily water use</span></div>
      <div class="card pad chart" id="chart-water">${legend([["Everything else", C.other], ["Sprinklers (estimated)", C.sprinkler]])}</div>
      <div class="section-title"><span>Weather</span></div>
      <div class="card pad chart" id="chart-weather">${legend([["Rain", C.rain], ["Evaporation", C.et, "line"]])}</div>
      <p class="note">Faded columns are the forecast. Evaporation is how much water a lawn loses to sun, heat and wind.</p>`;
    if (ins.has_water) {
      columnChart($("#chart-water"), { days: past.map((r) => r.day), unit: "gal", label: "Daily water use in gallons",
        stacks: [{ name: "Everything else", color: C.other, values: past.map((r) => r.other_gal ?? r.total_gal) },
                 { name: "Sprinklers (estimated)", color: C.sprinkler, values: past.map((r) => r.sprinkler_gal) }] });
    } else $("#chart-water").insertAdjacentHTML("beforeend", `<p class="note">Connect McKinney water in Settings.</p>`);
    if (ins.has_weather) {
      columnChart($("#chart-weather"), { days: ins.series.map((r) => r.day), unit: "in", digits: 2, label: "Daily rain and evaporation in inches",
        forecast: (d) => ins.series.find((r) => r.day === d)?.forecast,
        stacks: [{ name: "Rain", color: C.rain, values: ins.series.map((r) => r.rain_in) }],
        line: { name: "Evaporation", color: C.et, values: ins.series.map((r) => r.et0_in) } });
    } else $("#chart-weather").insertAdjacentHTML("beforeend", `<p class="note">Set your location above.</p>`);
  }
  body.insertAdjacentHTML("beforeend", `
    <div class="section-title"><span>Zones, last ${ins.days} days</span></div>
    ${ins.zones.length ? `<div class="table-wrap"><table class="viz-table"><thead><tr><th>Zone</th><th>Runs</th><th>Minutes</th><th>Gal/min</th><th>Gallons</th><th>Inches</th></tr></thead>
      <tbody>${ins.zones.map((z) => `<tr><td>${esc(z.label)}</td><td>${z.runs}</td><td>${fmtNum(z.minutes)}</td>
        <td>${z.gpm == null ? `<span class="muted">learning</span>` : `${fmtNum(z.gpm, 1)}`}</td><td>${fmtNum(z.gallons)}</td>
        <td>${z.inches == null ? (z.area_sqft ? "–" : `<span class="muted">add area</span>`) : fmtNum(z.inches, 2)}</td></tr>`).join("")}</tbody></table></div>
      <p class="note">Gal/min is worked out by matching each sprinkler run to the hourly water meter, after taking out the household's normal use for that hour. Inches need each zone's area (Sprinklers → zone settings).</p>`
      : `<p class="note">No sprinkler runs recorded yet. The hub logs every run it sees from now on.</p>`}
    ${ins.leak ? `<p class="note">Overnight use (1–5 AM, no watering): about ${fmtNum(ins.leak.gallons_per_hour, 1)} gal/hour over ${ins.leak.nights} nights${ins.leak.suspect ? ", which is high" : ", normal"}.</p>` : ""}`);
}


// ---------- Electricity view ----------
function fmtSlot(start, end) {
  const a = new Date(start), b = new Date(end);
  const t = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return `${a.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}, ${t(a)}–${t(b)}`;
}

async function renderEnergy(fetch = true) {
  if (fetch) state.energy = await api("GET", `/api/energy?days=${insightDays}`);
  const en = state.energy;
  const css = getComputedStyle(document.documentElement);
  const C = { devices: css.getPropertyValue("--viz-1").trim(), hvac: css.getPropertyValue("--viz-2").trim(), other: css.getPropertyValue("--viz-3").trim() };
  const s = en.summary;
  view.innerHTML = `${insightControls()}
    ${!en.has_energy ? `<p class="empty">No electricity data yet. Connect Smart Meter Texas in Settings, then tap <b>Fetch last 4 weeks</b>. Smart Meter Texas posts each day's 15-minute data the next day.</p>` : `
    <div class="tiles">
      <div class="tile"><span class="tile-label">Electricity</span><span class="tile-value">${compact(s.total_kwh)}</span><span class="tile-sub">kWh, ${en.series.length} days</span></div>
      <div class="tile"><span class="tile-label">Heating and cooling</span><span class="tile-value">${s.hvac_kwh == null || !s.total_kwh ? "–" : `${Math.round((s.hvac_kwh / s.total_kwh) * 100)}%`}</span><span class="tile-sub">${s.hvac_kwh == null ? "estimating from weather" : `${compact(s.hvac_kwh)} kWh, estimated`}</span></div>
      <div class="tile"><span class="tile-label">Always on</span><span class="tile-value">${en.standby_w == null ? "–" : compact(en.standby_w)}</span><span class="tile-sub">watts, overnight minimum</span></div>
      <div class="tile"><span class="tile-label">Highest 15 minutes</span><span class="tile-value">${s.peak_kw == null ? "–" : fmtNum(s.peak_kw, 1)}</span><span class="tile-sub">kW</span></div>
    </div>
    ${en.recommendations.length ? `<div class="section-title"><span>Suggestions</span></div>
    <div class="card">${en.recommendations.map((r) => `
      <div class="row rec"><span class="status status-${r.level}" aria-hidden="true">${STATUS_ICON[r.level]}</span>
        <div class="main"><span class="name">${esc(r.title)}</span><span class="sub wrap">${esc(r.text)}</span></div></div>`).join("")}</div>` : ""}
    <div id="en-body"></div>`}`;
  bindInsightControls();
  const body = $("#en-body");
  if (!body) return;
  const temp = (d) => { const r = en.series.find((x) => x.day === d); return r && r.tmean_f != null ? ` · ${Math.round(r.tmean_f)}°F` : ""; };
  if (insightTable) {
    body.innerHTML = `<div class="table-wrap"><table class="viz-table"><thead><tr><th>Day</th><th>Total (kWh)</th><th>Tracked devices</th><th>Heating and cooling (est.)</th><th>Everything else</th><th>Mean temp (°F)</th></tr></thead>
      <tbody>${en.series.map((r) => `<tr><td>${esc(shortDay(r.day))}</td><td>${fmtNum(r.total_kwh, 1)}</td><td>${fmtNum(r.devices_kwh, 1)}</td><td>${fmtNum(r.hvac_kwh, 1)}</td><td>${fmtNum(r.other_kwh, 1)}</td><td>${fmtNum(r.tmean_f)}</td></tr>`).join("")}</tbody></table></div>`;
  } else {
    body.innerHTML = `<div class="section-title"><span>Daily electricity</span></div>
      <div class="card pad chart" id="chart-energy">${legend([["Tracked devices", C.devices], ["Heating and cooling (estimated)", C.hvac], ["Everything else", C.other]])}</div>
      <p class="note">Heating and cooling is estimated from how daily use rises with outside temperature${en.hvac ? ` (fit R² ${en.hvac.r2})` : ""}. Hover a day for its temperature.</p>`;
    columnChart($("#chart-energy"), { days: en.series.map((r) => r.day), unit: "kWh", digits: 1, label: "Daily electricity in kWh", note: temp,
      stacks: [{ name: "Tracked devices", color: C.devices, values: en.series.map((r) => r.devices_kwh) },
               { name: "Heating and cooling (estimated)", color: C.hvac, values: en.series.map((r) => r.hvac_kwh || 0) },
               { name: "Everything else", color: C.other, values: en.series.map((r) => r.other_kwh) }] });
  }
  body.insertAdjacentHTML("beforeend", `
    <div class="section-title"><span>Biggest spikes</span></div>
    ${en.spikes.length ? `<div class="card">${en.spikes.map((e) => `
      <div class="row"><div class="main"><span class="name small">${esc(fmtSlot(e.start, e.end))}</span>
        <span class="sub wrap">${e.running.length ? `Running: ${esc(e.running.join(", "))}` : "Nothing tracked was running: likely the AC or furnace, an oven or the water heater"}</span></div>
        <span class="sub-value">${fmtNum(e.peak_kw, 1)} kW · ${fmtNum(e.kwh, 1)} kWh</span></div>`).join("")}</div>`
      : `<p class="note">No spikes stand out yet.</p>`}
    <div class="section-title"><span>Devices</span></div>
    ${en.devices.length ? `<div class="table-wrap"><table class="viz-table"><thead><tr><th>Device</th><th>Hours on</th><th>Power (kW)</th><th>kWh</th></tr></thead>
      <tbody>${en.devices.map((d) => `<tr><td>${esc(d.name)}</td><td>${fmtNum(d.hours, 1)}</td><td>${d.kw == null ? `<span class="muted">learning</span>` : fmtNum(d.kw, 2)}</td><td>${fmtNum(d.kwh, 1)}</td></tr>`).join("")}</tbody></table></div>
      <p class="note">Power is worked out by matching when each device ran against the meter's 15-minute readings. The hub records device activity from now on; electricity arrives a day late from Smart Meter Texas.</p>`
      : `<p class="note">No device activity recorded yet. The hub notes every 2 minutes which devices are running (washer, dryer, pool pump, AC, plugs, TV) from now on.</p>`}`);
}
