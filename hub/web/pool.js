// Insights → Pool: filter health from the pump's own power-at-flow readings, the cost of running it,
// and a log of fixes with before/after evidence. Insights → Electricity also gets the plan and bills here.

const POOL_LEVEL = {
  good: ["good", "Filter path clear", "The pump needs no more power than with a clean filter."],
  learning: ["neutral", "Learning the clean baseline", "The hub needs about a day of steady running at each flow to know what normal looks like."],
  warning: ["warning", "Check soon", "The pump is working harder than normal for the same flow."],
  serious: ["serious", "Act now", "The pump is struggling: it costs money and strains the motor."],
};
const POOL_STEP = { skimmer: "a", backwash: "b", filter_clean: "c", water_fill: "d" };
const money = (n) => (n == null ? "–" : `$${Number(n).toFixed(2)}`);
const clock = (m) => { const h = Math.floor((m % 1440) / 60), mm = m % 60; return `${h % 12 || 12}${mm ? `:${String(mm).padStart(2, "0")}` : ""} ${h < 12 ? "AM" : "PM"}`; };

function recsHtml(recs) {
  return recs.length ? `<div class="card">${recs.map((r) => `
    <div class="row rec"><span class="status status-${r.level}" aria-hidden="true">${STATUS_ICON[r.level] || "i"}</span>
      <div class="main"><span class="name">${esc(r.title)}</span><span class="sub wrap">${esc(r.text)}</span></div></div>`).join("")}</div>` : "";
}

async function renderPool(fetch = true) {
  if (fetch) state.pool = await api("GET", `/api/pool?days=${insightDays}`);
  if (state.tab !== "insights" || insightView !== "pool") return;
  const p = state.pool, h = p.health, live = p.live;
  if (!p.connected) {
    view.innerHTML = `${insightControls()}<p class="empty">Connect Pentair in Settings to watch the pool pump.</p>`;
    bindInsightControls(); return;
  }
  const [lvl, title, sub] = POOL_LEVEL[h.level] || POOL_LEVEL.learning;
  const freeNow = (() => { const hr = new Date().getHours(); return p.window.start > p.window.end ? hr >= p.window.start || hr < p.window.end : hr >= p.window.start && hr < p.window.end; })();
  const days = p.energy;
  const sum = (k) => days.reduce((a, d) => a + (d[k] || 0), 0);
  const asOf = live?.as_of ? new Date(live.as_of).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : null;
  view.innerHTML = `${insightControls()}
    ${live?.stale ? `<p class="note">Last reading from the pump was at ${esc(asOf)}; Pentair's cloud hasn't had a newer one.</p>` : ""}
    <div class="tiles">
      <div class="tile"><span class="tile-label">Flow</span><span class="tile-value">${live?.running ? fmtNum(live.gpm) : "Off"}</span><span class="tile-sub">${live?.running ? `GPM, target ${fmtNum(live.target)}` : "pump not running"}</span></div>
      <div class="tile"><span class="tile-label">Power</span><span class="tile-value">${live?.running ? fmtPower(live.watts) : "Off"}</span><span class="tile-sub">${freeNow ? "free hours" : "<b>paid hours</b>"}</span></div>
      <div class="tile"><span class="tile-label">Motor</span><span class="tile-value">${live?.running ? `${fmtNum(live.speed_pct)}%` : "–"}</span><span class="tile-sub">${live?.pressure_psi != null && live?.running ? `${fmtNum(live.pressure_psi, 1)} psi (pump's estimate)` : "speed"}</span></div>
      <div class="tile"><span class="tile-label">Power vs. clean</span><span class="tile-value">${h.ratio == null ? "–" : `${h.ratio >= 1 ? "+" : ""}${Math.round((h.ratio - 1) * 100)}%`}</span><span class="tile-sub">same flow, last 24 h</span></div>
    </div>

    <div class="section-title"><span>Filter and skimmer</span><span class="muted small">${p.samples.toLocaleString()} readings${p.first_sample ? ` since ${esc(new Date(p.first_sample).toLocaleDateString([], { month: "short", day: "numeric" }))}` : ""}</span></div>
    <div class="card">
      <div class="row rec"><span class="status status-${lvl}" aria-hidden="true">${STATUS_ICON[lvl] || "…"}</span>
        <div class="main"><span class="name">${esc(title)}</span><span class="sub wrap">${esc(sub)}</span></div></div>
      ${h.evidence.map((e) => `<div class="row"><span class="sub wrap">• ${esc(e)}</span></div>`).join("")}
      ${h.actions.length ? `<div class="row"><div class="main"><span class="name small">Do this, in order</span>
        <span class="sub wrap">${h.actions.map((a, i) => `${i + 1}. ${esc(a.label)} (${POOL_STEP[a.action]})`).join("<br>")}</span></div></div>` : ""}
      ${Object.keys(h.baseline).length ? `<div class="row"><span class="sub wrap">Clean baseline (${esc(h.baseline_from)}): ${Object.entries(h.baseline).map(([g, w]) => `${g} GPM → ${w} W`).join(", ")}</span></div>` : ""}
    </div>

    <div class="section-title"><span>Log what you did</span></div>
    <div class="card pad"><div class="inline wrap-btns">${p.actions.map((a) => `<button class="pill" data-pool-log="${a.action}">${POOL_STEP[a.action]}. ${esc(a.label)}</button>`).join("")}</div>
      <p class="note">Tap right after you do it. The hub compares the pump's power at the same flow in the day before and after, so you can see what actually helped.</p></div>
    ${p.maintenance.length ? `<div class="card">${p.maintenance.slice().reverse().map((m) => `
      <div class="row"><div class="main"><span class="name small">${esc(m.label)}</span>
        <span class="sub">${esc(new Date(m.at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }))}</span></div>
        <span class="sub-value">${m.effect ? `${m.effect.change_pct > 0 ? "+" : ""}${fmtNum(m.effect.change_pct, 1)}% power` : `<span class="muted">measuring…</span>`}</span>
        <button class="pill small" data-pool-del="${m.id}" aria-label="Remove this entry">✕</button></div>`).join("")}</div>` : ""}

    ${p.findings.length ? `<div class="section-title"><span>Running cost</span></div>${recsHtml(p.findings)}` : ""}

    <div class="section-title"><span>Pump programs</span><span class="muted small">free ${clock(p.window.start * 60)}–${clock(p.window.end * 60)}</span></div>
    <div class="table-wrap"><table class="viz-table"><thead><tr><th>Program</th><th>When</th><th>Flow</th><th>On</th><th>In paid hours</th></tr></thead>
      <tbody>${p.programs.map((g) => `<tr><td>${esc(g.name)}</td><td>${g.type === "schedule" ? `${clock(g.start_min)}–${clock(g.end_min)}` : esc(g.type)}</td>
        <td>${fmtNum(g.target)} ${g.target_kind === "gpm" ? "GPM" : "%"}</td><td>${g.enabled ? "Yes" : "No"}</td>
        <td>${g.paid_minutes == null ? "–" : g.paid_minutes ? `<b>${fmtDuration(g.paid_minutes)}</b>` : "none"}</td></tr>`).join("")}</tbody></table></div>
    <p class="note">Programs are read from the pump. Change them in the Pentair Home app.</p>

    <div class="section-title"><span>Pump electricity</span>${p.cost ? `<span class="muted small">about ${money(p.cost.per_month)} a month</span>` : ""}</div>
    ${days.length ? `<div class="table-wrap"><table class="viz-table"><thead><tr><th>Day</th><th>Free kWh</th><th>Paid kWh</th><th>Ran</th><th>Gallons</th></tr></thead>
      <tbody>${days.slice().reverse().map((d) => `<tr><td>${esc(shortDay(d.day))}</td><td>${fmtNum(d.free_kwh, 2)}</td><td>${d.paid_kwh ? `<b>${fmtNum(d.paid_kwh, 2)}</b>` : "0"}</td>
        <td>${fmtDuration((d.free_h + d.paid_h) * 60)}</td><td>${compact(d.gallons)}</td></tr>`).join("")}
        <tr><td><b>Total</b></td><td>${fmtNum(sum("free_kwh"), 1)}</td><td>${fmtNum(sum("paid_kwh"), 1)}</td><td>${fmtDuration((sum("free_h") + sum("paid_h")) * 60)}</td><td>${compact(sum("gallons"))}</td></tr></tbody></table></div>`
      : `<p class="note">Recorded from now on, every 2 minutes.</p>`}
    ${p.efficiency.length ? `<div class="table-wrap"><table class="viz-table"><thead><tr><th>Flow</th><th>Power</th><th>Gallons per kWh</th></tr></thead>
      <tbody>${p.efficiency.map((e) => `<tr><td>${fmtNum(e.gpm)} GPM</td><td>${e.watts} W</td><td>${e.gal_per_kwh.toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
  bindInsightControls();
  view.querySelectorAll("[data-pool-log]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api("POST", "/api/pool/maintenance", { action: b.dataset.poolLog }); toast("Logged. The before/after shows up after a day of running."); await renderPool(true); }
    catch (err) { toast(err.message); b.disabled = false; }
  }));
  view.querySelectorAll("[data-pool-del]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = "Remove?"; return; }
    try { await api("DELETE", `/api/pool/maintenance/${b.dataset.poolDel}`); await renderPool(true); } catch (err) { toast(err.message); }
  }));
}

// Electricity view: plan, bills and free/paid hours.
async function planHtml(signal) {
  const [tou, bills, rc] = await Promise.all([api("GET", `/api/tou?days=${insightDays}`, undefined, { signal }), api("GET", "/api/bills", undefined, { signal }),
    api("GET", "/api/reconcile", undefined, { signal }).catch(() => null)]);
  return reconcileHtml(rc) + planBody(tou, bills);
}

// Each bill against the meter: where the dollars went by part of the day, and savings against last year.
const PART_COLOR = { night: "var(--viz-1)", morning: "var(--viz-2)", afternoon: "var(--viz-3)", evening: "var(--status-warning)" };
const usd0 = (n) => (n == null ? "–" : `${n < 0 ? "−" : ""}$${Math.abs(Math.round(n)).toLocaleString()}`);
const monthOf = (r) => new Date(`${r.period_end}T12:00`).toLocaleDateString([], { month: "short", year: "numeric" });

function reconcileHtml(rc) {
  if (!rc || !rc.bills?.length) return "";
  const bills = rc.bills.slice().reverse();
  const latest = bills.find((b) => b.complete);
  const s = rc.savings || {};
  const partTotal = (b, k) => b.parts.find((x) => x.part === k)?.total;
  const bar = (b) => {
    const sum = b.parts.reduce((a, x) => a + x.total, 0) || 1;
    return `<div class="part-bar" role="img" aria-label="Share of the bill by part of the day">${b.parts.map((x) =>
      `<i style="width:${(x.total / sum) * 100}%;background:${PART_COLOR[x.part]}" title="${esc(x.label)} ${money(x.total)}"></i>`).join("")}</div>`;
  };
  const detail = (b) => `
    <details class="card pad" ${b === latest ? "open" : ""}>
      <summary><b>${esc(monthOf(b))}</b> · billed ${money(b.billed_total)}${b.complete
        ? ` · night ${usd0(partTotal(b, "night"))}, evening ${usd0(partTotal(b, "evening"))}`
        : ` · <span class="muted">meter has ${Math.round(b.coverage * 100)}% of this period${rc.missing_days ? ", fetching the rest" : ""}</span>`}</summary>
      ${b.complete ? bar(b) : ""}
      <p class="note">${esc(shortDay(b.period_start))} – ${esc(shortDay(b.period_end))}, ${b.days} days. The bill's own kWh and charges, split by part of the day as the meter recorded them${b.complete ? `; the parts add up to the bill (${b.difference >= 0 ? "+" : "−"}${money(Math.abs(b.difference))})` : ""}.
        Check: meter ${fmtNum(b.meter_kwh)} kWh vs billed ${fmtNum(b.billed_kwh)}; free nights ${fmtNum(b.meter_free_kwh)} vs billed ${fmtNum(b.billed_free_kwh)}${b.complete ? `; priced straight from the meter ${money(b.meter_priced_total)}` : ""}.</p>
      <div class="table-wrap"><table class="viz-table"><thead><tr><th>Part of day</th><th>kWh</th><th>Energy</th><th>Delivery</th><th>Fees</th><th>Total</th><th>¢/kWh</th>${b.seasonal ? "<th>Seasonal extra</th>" : ""}</tr></thead>
        <tbody>${b.parts.map((x) => `<tr><td><i class="viz-swatch" style="background:${PART_COLOR[x.part]}"></i>${esc(x.label)} <span class="muted">${esc(x.hours)}</span></td>
          <td>${fmtNum(x.kwh)}</td><td>${x.energy ? money(x.energy) : "free"}</td><td>${money(x.delivery)}</td><td>${money(x.fees)}</td><td><b>${money(x.total)}</b></td>
          <td>${x.cents_per_kwh ?? "–"}</td>${b.seasonal ? `<td>${money(x.seasonal_usd)}</td>` : ""}</tr>`).join("")}
          <tr><td>Base charge</td><td></td><td></td><td></td><td></td><td>${money(b.base)}</td><td></td>${b.seasonal ? "<td></td>" : ""}</tr>
          <tr><td><b>Total</b></td><td>${fmtNum(b.meter_kwh)}</td><td>${money(b.energy_total)}</td><td>${money(b.delivery_total)}</td><td>${money(b.fees_total)}</td>
            <td><b>${money(b.rebuilt_total)}</b></td><td></td>${b.seasonal ? `<td><b>${money(b.seasonal.usd)}</b></td>` : ""}</tr></tbody></table></div>
      ${b.vs_last_year ? `<p class="note">Against the same period last year, at this bill's prices: <b>${b.vs_last_year.saved >= 0 ? `${money(b.vs_last_year.saved)} saved` : `${money(-b.vs_last_year.saved)} more`}</b>
        (${b.vs_last_year.kwh_change > 0 ? "+" : ""}${fmtNum(b.vs_last_year.kwh_change)} kWh). Weather differs from year to year, so read hot or cold months with that in mind.</p>` : ""}
    </details>`;
  return `
    <div class="section-title"><span>Where each bill went</span><span class="muted small">your bills × the meter, by part of the day</span></div>
    <div class="tiles">
      <div class="tile"><span class="tile-label">Savings goal</span><span class="tile-value">${usd0(rc.target)}</span><span class="tile-sub">a month, against the same month last year</span></div>
      <div class="tile"><span class="tile-label">Saved so far</span><span class="tile-value">${s.months ? usd0(s.average) : "–"}</span><span class="tile-sub">${s.months ? `a month on average, ${s.months} bill(s), ${usd0(s.total)} in all` : "starts with your next bill, compared with a year earlier"}</span></div>
      ${latest ? `<div class="tile"><span class="tile-label">Delivery, ${esc(monthOf(latest))}</span><span class="tile-value">${usd0(latest.delivery_total)}</span><span class="tile-sub">${usd0(partTotal(latest, "night"))} of it at night, when energy is free</span></div>
      <div class="tile"><span class="tile-label">Seasonal extra</span><span class="tile-value">${latest.seasonal ? usd0(latest.seasonal.usd) : "–"}</span><span class="tile-sub">above your mildest month: mostly heating and cooling</span></div>` : ""}
    </div>
    ${legend(rc.parts.map((p) => [`${p.label} ${p.hours}`, PART_COLOR[p.part]]))}
    ${bills.map(detail).join("")}
    <div class="table-wrap"><table class="viz-table"><thead><tr><th>Bill</th><th>Billed</th><th>Night</th><th>Morning</th><th>Afternoon</th><th>Evening</th><th>Seasonal</th><th>vs last year</th></tr></thead>
      <tbody>${bills.map((b) => `<tr><td>${esc(monthOf(b))}</td><td><b>${usd0(b.billed_total)}</b></td>${b.complete
        ? ["night", "morning", "afternoon", "evening"].map((k) => `<td>${usd0(partTotal(b, k))}</td>`).join("") + `<td>${b.seasonal ? usd0(b.seasonal.usd) : "–"}</td>`
        : `<td colspan="5" class="muted">meter ${Math.round(b.coverage * 100)}%</td>`}<td>${b.vs_last_year ? usd0(b.vs_last_year.saved) : "–"}</td></tr>`).join("")}</tbody></table></div>`;
}

function planBody(tou, bills) {
  const t = bills.tariff;
  const maxH = tou.hours ? Math.max(...tou.hours.map((h) => h.kwh)) : 1;
  return `
    <div class="section-title"><span>Your plan</span>${t.plan ? `<span class="muted small">${esc(t.plan)}</span>` : ""}</div>
    ${t.energy_cents != null ? `<div class="tiles">
      <div class="tile"><span class="tile-label">Paid hours</span><span class="tile-value">${fmtNum(t.energy_cents + t.delivery_cents, 1)}¢</span><span class="tile-sub">per kWh: ${fmtNum(t.energy_cents, 1)}¢ energy + ${fmtNum(t.delivery_cents, 1)}¢ delivery</span></div>
      <div class="tile"><span class="tile-label">Free hours</span><span class="tile-value">${fmtNum(t.night_delivery_cents, 1)}¢</span><span class="tile-sub">per kWh, delivery only, ${clock(t.free_start * 60)}–${clock(t.free_end * 60)}</span></div>
      ${tou.paid_kwh != null ? `<div class="tile"><span class="tile-label">In paid hours</span><span class="tile-value">${Math.round((tou.paid_kwh / (tou.paid_kwh + tou.free_kwh)) * 100)}%</span><span class="tile-sub">${fmtNum(tou.paid_kwh / tou.days, 1)} kWh a day</span></div>` : ""}
      ${tou.cost ? `<div class="tile"><span class="tile-label">Estimated</span><span class="tile-value">${money(tou.cost.per_month)}</span><span class="tile-sub">a month at this rate, from the meter</span></div>` : ""}
    </div>` : `<p class="note">Import your TXU bills to price your use: run <code>python -m hub.bills import ~/Downloads/"TXU Invoices"</code> on the Mac.</p>`}
    ${recsHtml([...(tou.recommendations || []), ...bills.recommendations])}
    ${tou.hours ? `<div class="section-title"><span>Average day</span><span class="muted small">kWh per hour, last ${tou.days} days</span></div>
    <div class="card pad"><div class="hour-bars" role="img" aria-label="Average kWh by hour of day">${tou.hours.map((h) => `
      <div class="hb ${h.free ? "free" : "paid"}" title="${clock(h.hour * 60)}: ${h.kwh} kWh${h.free ? " (free)" : ""}"><i style="height:${Math.max(2, (h.kwh / maxH) * 100)}%"></i><span>${h.hour % 6 === 0 ? clock(h.hour * 60).replace(" ", "") : ""}</span></div>`).join("")}</div>
      ${legend([["Paid hours", "var(--status-warning)"], ["Free hours", "var(--viz-1)"]])}</div>` : ""}
    ${bills.bills.length ? `<div class="section-title"><span>Bills</span><span class="muted small">from your TXU PDFs</span></div>
    <div class="table-wrap"><table class="viz-table"><thead><tr><th>Period to</th><th>kWh</th><th>Free</th><th>Energy</th><th>Delivery</th><th>Total</th><th>¢/kWh</th></tr></thead>
      <tbody>${bills.bills.slice().reverse().map((b) => `<tr><td>${esc(shortDay(b.period_end))}</td><td>${b.kwh.toLocaleString()}</td><td>${Math.round(b.free_share * 100)}%</td>
        <td>${money(b.energy_net)}</td><td>${money(b.delivery_net)}${b.tdu_credit ? ` <span class="muted">(−${money(b.tdu_credit)} night)</span>` : ""}</td><td><b>${money(b.total)}</b></td><td>${fmtNum(b.all_in_cents, 1)}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}
