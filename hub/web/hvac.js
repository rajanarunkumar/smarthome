// Insights → HVAC: heating and cooling run time from the Nest and Lennox thermostats, split into TXU's
// free and paid hours, priced from the bill, with a health check from run time per degree-day.

const HVAC_LEVEL = {
  good: ["good", "Running normally", "Run time for the weather is at this system's clean level."],
  learning: ["neutral", "Learning the clean baseline", "The hub needs about 5 days of hot (or cold) weather with the thermostat online."],
  warning: ["warning", "Check soon", "The system is running longer than it should for the weather."],
  serious: ["serious", "Act now", "The system is struggling: it costs money and wears the compressor."],
};
const hrs = (m) => fmtDuration(m);

function hvacSetupHtml(connected) {
  return `
    <div class="section-title"><span>Connect your thermostats</span></div>
    <div class="card">
      <div class="row"><div class="main"><span class="name">Lennox iComfort ${connected.lennox ? "✓" : ""}</span>
        <span class="sub wrap">Settings → <b>Lennox iComfort</b>: the email and password you use in the Lennox iComfort app, then <b>Connect</b>.
        For a connection that stays on your Wi‑Fi, also enter the thermostat's IP address (on the thermostat: Menu → Settings → Wi‑Fi → Advanced, or your router's device list).
        Works with S30, E30, M30 and S40.</span></div></div>
      <div class="row"><div class="main"><span class="name">Nest ${connected.nest ? "✓" : ""}</span>
        <span class="sub wrap">Google requires its Device Access program (a one-time US$5 fee, about 15 minutes):<br>
        1. Register at console.nest.google.com/device-access with the Google account that owns the thermostat.<br>
        2. In console.cloud.google.com, enable <b>Smart Device Management API</b>.<br>
        3. Create an OAuth client (Web application). Add this app's address with a trailing slash as an authorized redirect URI: <code>${esc(location.origin)}/</code>. Set the consent screen to <b>In production</b>.<br>
        4. In Device Access, create a project with that client ID and copy the project ID.<br>
        5. Settings → <b>Nest thermostat</b>: paste the project ID, client ID and secret, tap <b>Save and link</b>, and tick the thermostat.</span></div></div>
      <div class="row"><button class="pill primary" id="hvac-settings">Open Settings</button></div>
    </div>`;
}

async function renderHvac(fetch = true) {
  if (fetch) state.hvac = await api("GET", `/api/hvac?days=${insightDays}`);
  if (state.tab !== "insights" || insightView !== "hvac") return;
  const p = state.hvac;
  const anyConnected = p.connected.nest || p.connected.lennox;
  if (!p.systems.length) {
    view.innerHTML = `${insightControls()}
      ${anyConnected ? `<p class="note">Connected. Readings start within 2 minutes; the health check needs about 5 days of weather that needs heating or cooling.</p>` : ""}
      ${hvacSetupHtml(p.connected)}`;
    bindInsightControls();
    $("#hvac-settings")?.addEventListener("click", () => switchTab("settings"));
    return;
  }
  view.innerHTML = `${insightControls()}
    ${p.recommendations.length ? `<div class="section-title"><span>Cost</span><span class="muted small">${p.prices ? `${fmtNum(p.prices.paid, 1)}¢ paid · ${fmtNum(p.prices.free, 1)}¢ free per kWh` : `free ${clock(p.window.start * 60)}–${clock(p.window.end * 60)}`}</span></div>${recsHtml(p.recommendations)}` : ""}
    ${p.systems.map(systemHtml).join("")}
    <div class="section-title"><span>Log what you did</span></div>
    <div class="card pad">
      ${p.systems.length > 1 ? `<div class="row"><select id="hvac-dev" aria-label="Which system"><option value="">All systems</option>${p.systems.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select></div>` : ""}
      <div class="inline wrap-btns">${p.actions.map((a) => `<button class="pill" data-hvac-log="${a.action}">${esc(a.label)}</button>`).join("")}</div>
      <p class="note">Tap right after you do it. The hub compares run time per degree-day in the 5 days before and after, so you can see what helped.</p></div>
    ${anyConnected && !(p.connected.nest && p.connected.lennox) ? `<details class="card pad"><summary>Connect the other thermostat</summary>${hvacSetupHtml(p.connected)}</details>` : ""}`;
  bindInsightControls();
  $("#hvac-settings")?.addEventListener("click", () => switchTab("settings"));
  view.querySelectorAll("[data-hvac-log]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await api("POST", "/api/hvac/maintenance", { action: b.dataset.hvacLog, device_id: $("#hvac-dev")?.value || (p.systems.length === 1 ? p.systems[0].id : null) });
      toast("Logged. The before/after shows up after 5 days of hot or cold weather."); await renderHvac(true);
    } catch (err) { toast(err.message); b.disabled = false; }
  }));
  view.querySelectorAll("[data-hvac-del]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = "Remove?"; return; }
    try { await api("DELETE", `/api/hvac/maintenance/${b.dataset.hvacDel}`); await renderHvac(true); } catch (err) { toast(err.message); }
  }));
}

function systemHtml(s) {
  const h = s.health, l = s.live || {};
  const [lvl, title, sub] = HVAC_LEVEL[h.level] || HVAC_LEVEL.learning;
  const now = l.hvac === "COOLING" ? "Cooling" : l.hvac === "HEATING" ? "Heating" : l.online === false ? "Offline" : "Idle";
  const set = l.mode === "COOL" ? l.cool : l.mode === "HEAT" ? l.heat : l.mode === "HEATCOOL" ? `${l.heat}–${l.cool}` : "Off";
  const dd = h.mode === "cool" ? "per_cdd" : "per_hdd";
  const c = s.cost, priced = !!c, n = Math.max(s.days.length, 1);
  return `
    <div class="section-title"><span>${esc(s.name)}</span><span class="muted small">${priced ? `about ${money(c.per_day)} a day` : "learning its cost from the meter"}</span></div>
    <div class="tiles">
      <div class="tile"><span class="tile-label">Now</span><span class="tile-value">${now}</span><span class="tile-sub">${l.current != null ? `${l.current} °F inside` : ""}${l.humidity != null ? ` · ${l.humidity}%` : ""}</span></div>
      <div class="tile"><span class="tile-label">Set to</span><span class="tile-value">${set ?? "–"}${typeof set === "number" ? "°" : ""}</span><span class="tile-sub">${esc((l.mode || "").toLowerCase())}</span></div>
      <div class="tile"><span class="tile-label">Paid hours</span><span class="tile-value">${priced ? `$${Math.round(c.paid_month)}` : hrs(s.paid_min / n)}</span><span class="tile-sub">${priced ? `a month, ${clock(state.hvac.window.end * 60)}–${clock(state.hvac.window.start * 60)}` : "a day"}</span></div>
      <div class="tile"><span class="tile-label">Free hours</span><span class="tile-value">${priced ? `$${Math.round(c.free_month)}` : hrs(s.free_min / n)}</span><span class="tile-sub">${priced ? "a month, delivery only" : "a day"}</span></div>
    </div>
    <div class="card">
      <div class="row rec"><span class="status status-${lvl}" aria-hidden="true">${STATUS_ICON[lvl] || "…"}</span>
        <div class="main"><span class="name">${esc(title)}</span><span class="sub wrap">${esc(sub)}</span></div></div>
      ${h.evidence.map((e) => `<div class="row"><span class="sub wrap">• ${esc(e)}</span></div>`).join("")}
      ${h.actions.length ? `<div class="row"><div class="main"><span class="name small">Do this, in order</span>
        <span class="sub wrap">${h.actions.map((a, i) => `${i + 1}. ${esc(a.label)}`).join("<br>")}</span></div></div>` : ""}
    </div>
    ${s.maintenance.length ? `<div class="card">${s.maintenance.slice().reverse().map((m) => `
      <div class="row"><div class="main"><span class="name small">${esc(m.label)}</span>
        <span class="sub">${esc(new Date(m.at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }))}</span></div>
        <span class="sub-value">${m.effect ? `${m.effect.change_pct > 0 ? "+" : ""}${fmtNum(m.effect.change_pct, 1)}% run time` : `<span class="muted">measuring…</span>`}</span>
        <button class="pill small" data-hvac-del="${m.id}" aria-label="Remove this entry">✕</button></div>`).join("")}</div>` : ""}
    ${s.days.length ? `<div class="table-wrap"><table class="viz-table"><thead><tr><th>Day</th><th>Outside</th>${priced
        ? "<th>Paid kWh</th><th>Paid $</th><th>Free kWh</th><th>Free $</th><th>Total</th>"
        : "<th>Cool</th><th>Heat</th><th>Paid</th>"}<th>Runs</th><th>Min/°day</th></tr></thead>
      <tbody>${s.days.slice().reverse().map((d) => `<tr><td>${esc(shortDay(d.day))}</td><td>${d.tmean_f != null ? `${Math.round(d.tmean_f)}°` : "–"}</td>
        ${priced ? `<td>${fmtNum(d.paid_kwh, 1)}</td><td>${d.paid_usd ? `<b>${money(d.paid_usd)}</b>` : "$0"}</td><td>${fmtNum(d.free_kwh, 1)}</td><td>${money(d.free_usd)}</td><td>${money(d.paid_usd + d.free_usd)}</td>`
          : `<td>${hrs(d.cool_min)}</td><td>${hrs(d.heat_min)}</td><td>${d.paid_min ? `<b>${hrs(d.paid_min)}</b>` : "0"}</td>`}
        <td>${d.cycles}${d.short ? ` <span class="muted">(${d.short} short)</span>` : ""}</td><td>${d[dd] ?? "–"}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}
