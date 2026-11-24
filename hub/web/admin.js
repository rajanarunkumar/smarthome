"use strict";
// Admin tab: hub health, brands, caches, background jobs, requests, logs, and manual refreshes.

let adminData = null;
let adminResult = null;          // { ok, text } from the last manual action
let adminLogBrand = "";
const adminBusy = new Map();     // action key -> start time (ms)
let touching = false;            // don't redraw under a finger

document.addEventListener("pointerdown", () => { touching = true; }, { passive: true });
["pointerup", "pointercancel"].forEach((t) => document.addEventListener(t, () => { setTimeout(() => { touching = false; }, 150); }, { passive: true }));

const fmtMs = (v) => v == null ? "–" : v >= 10000 ? `${(v / 1000).toFixed(0)} s` : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
const fmtBytes = (b) => b == null ? "–" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : b >= 1e3 ? `${(b / 1e3).toFixed(0)} KB` : `${b} B`;
function fmtDur(s) {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} m` : m ? `${m} m` : `${Math.round(s)} s`;
}
function ago(t) {
  if (!t) return "never";
  const at = typeof t === "string" ? Date.parse(t) / 1000 : t;
  const s = Date.now() / 1000 - at;
  if (s < 0) return `in ${fmtDur(-s)}`;
  return s < 5 ? "just now" : `${fmtDur(s)} ago`;
}
const lvl = (v, warn, bad) => v == null ? "off" : v >= bad ? "bad" : v >= warn ? "warn" : "ok";
const dot = (cls, label = "") => `<span class="dot ${cls}" ${label ? `role="img" aria-label="${esc(label)}"` : 'aria-hidden="true"'}></span>`;

function spark(series, key) {
  const pts = series.map((p) => p[key]).filter((v) => v != null);
  if (pts.length < 2) return "";
  const max = Math.max(...pts) || 1, n = pts.length;
  const xy = pts.map((v, i) => `${((i / (n - 1)) * 100).toFixed(2)},${(30 - (v / max) * 28).toFixed(2)}`);
  return `<svg class="adm-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
    <path class="area" d="M0,30 L${xy.join(" L")} L100,30 Z"/><path d="M${xy.join(" L")}"/></svg>`;
}

function busyButton(key, label, attrs, cls = "pill") {
  const start = adminBusy.get(key);
  if (start) return `<button class="${cls}" disabled>${label}… <span class="elapsed" data-since="${start}">${Math.round((Date.now() - start) / 1000)} s</span></button>`;
  return `<button class="${cls}" ${attrs}>${label}</button>`;
}

async function renderAdmin({ fetch = true, signal, live = () => true } = {}) {
  if (adminData) drawAdmin(); else setView(skeleton(6));
  if (!fetch) return;
  const data = await api("GET", "/api/admin/telemetry", undefined, { signal });
  adminData = data;
  if (live() && !touching) drawAdmin();
}

function brandHealth(b) {
  if (!b.active) return "off";
  const op = b.ops.devices || {};
  if (b.cache?.stale || (op.last_error_at && (!op.last_ok || op.last_error_at > op.last_ok))) return "bad";
  if ((op.p95_ms || 0) > 4000 || (b.cache && b.cache.age_s > 120)) return "warn";
  if (!b.cache) return "warn";  // connected but no answer yet
  return "ok";
}

function drawAdmin() {
  const d = adminData;
  if (!d) return;
  const p = d.process, h = d.host, L = d.loop, S = d.series || [];
  const dbSize = (h.files?.["hub.db"] || 0) + (h.files?.["hub.db-wal"] || 0);
  const devRoute = d.requests.routes.find((r) => r.route === "GET /api/devices");
  const activeBrands = d.brands.filter((b) => b.active && b.brand !== "hub");
  const badBrands = activeBrands.filter((b) => brandHealth(b) === "bad").length;
  const label = Object.fromEntries(d.brands.map((b) => [b.brand, b.label]));
  const logBrands = [...new Set(d.logs.map((l) => l.logger.split(".").pop().replace(/_adapter$/, "")))].sort();
  const logs = d.logs.filter((l) => !adminLogBrand || l.logger.includes(adminLogBrand));

  setView(`
    <div class="adm-actions">
      ${busyButton("raw", "Refresh raw data", 'data-adm="raw"', "pill primary")}
      ${busyButton("backfill", "Fetch last 4 weeks", 'data-adm="backfill"')}
      ${busyButton("cache-reload", "Reload caches", 'data-adm="cache" data-reload="1"')}
      ${busyButton("cache-clear", "Clear caches", 'data-adm="cache" data-reload="0"')}
      <button class="pill" data-update-open>Update or restart…</button>
    </div>
    ${adminResult ? `<div class="adm-result ${adminResult.ok ? "" : "bad"}" role="status">${adminResult.html}</div>` : ""}

    <div class="adm-grid">
      <div class="adm-card"><div class="adm-label">${dot("ok")} Hub up</div><div class="adm-value">${fmtDur(p.uptime_s)}</div>
        <div class="adm-sub">${esc(p.commit || "")} · Python ${esc(p.python)} · pid ${p.pid}</div></div>
      <div class="adm-card"><div class="adm-label">${dot(lvl(p.cpu_pct, 50, 90))} CPU</div><div class="adm-value">${p.cpu_pct ?? "–"}%</div>
        <div class="adm-sub">load ${h.load_avg.join(" · ")}</div>${spark(S, "cpu_pct")}</div>
      <div class="adm-card"><div class="adm-label">${dot(lvl(p.rss_mb, 600, 1200))} Memory</div><div class="adm-value">${p.rss_mb ?? "–"} MB</div>
        <div class="adm-sub">peak ${p.peak_rss_mb} MB</div>${spark(S, "rss_mb")}</div>
      <div class="adm-card"><div class="adm-label">${dot(lvl(L.lag_p95_ms, 50, 250))} Event loop lag</div><div class="adm-value">${fmtMs(L.lag_p95_ms)}</div>
        <div class="adm-sub">p95 over 5 min · max ${fmtMs(L.lag_max_ms)}</div>${spark(S, "lag_p95_ms")}</div>
      <div class="adm-card"><div class="adm-label">${dot("ok")} Requests</div><div class="adm-value">${d.requests.per_min}/min</div>
        <div class="adm-sub">${d.requests.slow.length} slow (over 1 s) recently</div>${spark(S, "req_per_min")}</div>
      <div class="adm-card"><div class="adm-label">${dot(lvl(devRoute?.p95_ms, 1000, 4500))} Device list</div><div class="adm-value">${fmtMs(devRoute?.p95_ms)}</div>
        <div class="adm-sub">p95 · ${devRoute?.count ?? 0} calls</div>${spark(S, "devices_p95_ms")}</div>
      <div class="adm-card"><div class="adm-label">${dot(badBrands ? "bad" : "ok")} Brands</div><div class="adm-value">${activeBrands.length - badBrands}/${activeBrands.length}</div>
        <div class="adm-sub">${badBrands ? `${badBrands} with problems` : "all answering"}</div></div>
      <div class="adm-card"><div class="adm-label">${dot(lvl(h.disk_free_gb == null ? null : 100 - h.disk_free_gb, 90, 95))} Disk free</div><div class="adm-value">${h.disk_free_gb ?? "–"} GB</div>
        <div class="adm-sub">of ${h.disk_total_gb ?? "–"} GB</div></div>
      <div class="adm-card"><div class="adm-label">${dot("ok")} Database</div><div class="adm-value">${fmtBytes(dbSize)}</div>
        <div class="adm-sub">log ${fmtBytes(h.files?.["logs/hub.log"])} · photos ${fmtBytes(h.files?.["photos/"])}</div></div>
      <div class="adm-card"><div class="adm-label">${dot("ok")} Process</div><div class="adm-value">${p.threads} thr</div>
        <div class="adm-sub">${p.asyncio_tasks ?? "–"} tasks · ${p.open_fds ?? "–"} files open</div></div>
    </div>

    <div class="adm-section"><h2>Brands</h2><span class="adm-sub">tap Refresh to read one brand directly</span></div>
    <div class="card">${d.brands.filter((b) => b.brand !== "hub").map((b) => {
      const op = b.ops.devices || {}, set = b.ops.set_state;
      const health = brandHealth(b);
      const err = b.cache?.stale ? b.cache.error : (op.last_error_at && (!op.last_ok || op.last_error_at > op.last_ok)) ? op.last_error : null;
      return `<div class="adm-row">
        ${dot(health, health)}
        <div class="main"><div class="name">${esc(b.label)}</div>
          <div class="adm-sub">${b.active ? [
            b.cache ? `${b.cache.devices} device${b.cache.devices === 1 ? "" : "s"}, read ${fmtDur(b.cache.age_s)} ago` : "not read yet",
            op.calls ? `last ${fmtMs(op.last_ms)}, p95 ${fmtMs(op.p95_ms)}` : "",
            op.errors ? `${op.errors} error${op.errors === 1 ? "" : "s"} of ${op.calls}` : "",
            set?.calls ? `${set.calls} change${set.calls === 1 ? "" : "s"}, p95 ${fmtMs(set.p95_ms)}` : "",
          ].filter(Boolean).join(" · ") : "not connected"}</div>
          ${err ? `<div class="adm-err">${esc(err)}</div>` : ""}</div>
        ${b.active ? busyButton(`brand:${b.brand}`, "Refresh", `data-adm="brand" data-brand="${esc(b.brand)}"`, "pill small") : ""}
      </div>`;
    }).join("")}</div>

    <div class="adm-section"><h2>Caches</h2><span class="adm-sub">in memory, refreshed in the background</span></div>
    <div class="card">${d.caches.map((c) => `<div class="adm-row">
        ${dot(c.load_failures && c.entries.some((e) => e.stale) ? "warn" : "ok")}
        <div class="main"><div class="name">${esc(c.name)}</div>
          <div class="adm-sub">${c.size} entr${c.size === 1 ? "y" : "ies"} · hit rate ${c.hit_rate == null ? "–" : Math.round(c.hit_rate * 100) + "%"}
            · ${c.loads} loads, ${c.load_failures} failed · p95 load ${fmtMs(c.load_ms_p95)} · oldest ${c.oldest_s == null ? "–" : fmtDur(c.oldest_s)}
            · refresh ${c.refresh_after_s} s / expire ${fmtDur(c.expire_after_s)}${c.timeouts ? ` · ${c.timeouts} timeouts` : ""}${c.stale_serves ? ` · ${c.stale_serves} stale serves` : ""}</div></div>
        ${busyButton(`cache:${c.name}:1`, "Reload", `data-adm="cache" data-cache="${esc(c.name)}" data-reload="1"`, "pill small")}
        ${busyButton(`cache:${c.name}:0`, "Clear", `data-adm="cache" data-cache="${esc(c.name)}" data-reload="0"`, "pill small")}
      </div>`).join("")}</div>

    <div class="adm-section"><h2>Background jobs</h2></div>
    <div class="card">${Object.keys(d.jobs).length ? Object.entries(d.jobs).map(([name, j]) => `<div class="adm-row">
        ${dot(j.ok === false ? "bad" : "ok")}
        <div class="main"><div class="name">${esc(name)}</div>
          <div class="adm-sub">${[j.last_start ? `ran ${ago(j.last_start)}` : j.last_at ? `ran ${ago(j.last_at)}` : "",
            j.last_ms != null ? `took ${fmtMs(j.last_ms)}` : "", j.runs ? `${j.runs} runs${j.failures ? `, ${j.failures} failed` : ""}` : "",
            j.next_run ? `next ${ago(j.next_run)}` : ""].filter(Boolean).join(" · ")}</div>
          ${j.detail ? `<div class="${j.ok === false ? "adm-err" : "adm-sub"}">${esc(j.detail)}</div>` : ""}</div></div>`).join("")
      : `<div class="adm-row"><span class="adm-sub">No runs yet since the hub started.</span></div>`}</div>

    <div class="adm-section"><h2>Requests</h2><span class="adm-sub">since start · slowest first</span></div>
    <div class="adm-scroll"><table class="adm-table">
      <thead><tr><th>Route</th><th>Calls</th><th>p50</th><th>p95</th><th>Max</th><th>Errors</th></tr></thead>
      <tbody>${d.requests.routes.slice(0, 25).map((r) => `<tr><td>${esc(r.route)}</td><td>${r.count}</td><td>${fmtMs(r.p50_ms)}</td>
        <td>${fmtMs(r.p95_ms)}</td><td>${fmtMs(r.max_ms)}</td><td>${r.err5 ? `<b class="adm-err">${r.err5}</b>` : ""}${r.err5 && r.err4 ? " / " : ""}${r.err4 || (r.err5 ? "" : "0")}</td></tr>`).join("")}</tbody>
    </table></div>
    ${d.requests.slow.length ? `<div class="adm-section"><h2>Slow requests</h2></div>
    <div class="card">${d.requests.slow.slice(0, 10).map((r) => `<div class="adm-row"><div class="main">
      <div class="adm-log">${esc(r.route)} → ${r.status}</div><div class="adm-sub">${ago(r.t)}</div></div><b>${fmtMs(r.ms)}</b></div>`).join("")}</div>` : ""}

    <div class="adm-section"><h2>Recent activity</h2><span class="adm-sub">manual refreshes</span></div>
    <div class="card">${d.activity.length ? d.activity.map((a) => `<div class="adm-row">${dot(a.ok ? "ok" : "bad")}
      <div class="main"><div class="name small">${esc(a.what)}</div><div class="adm-sub">${ago(a.t)} · ${fmtMs(a.ms)} · ${esc(a.result)}</div></div></div>`).join("")
      : `<div class="adm-row"><span class="adm-sub">Nothing yet.</span></div>`}</div>

    <div class="adm-section"><h2>Warnings and errors</h2>
      <select id="adm-log-brand" aria-label="Filter by source" class="adm-sub">
        <option value="">All sources</option>${logBrands.map((b) => `<option value="${esc(b)}" ${b === adminLogBrand ? "selected" : ""}>${esc(label[b] || b)}</option>`).join("")}
      </select></div>
    <div class="card">${logs.length ? logs.slice(0, 50).map((l) => `<div class="adm-row"><div class="main adm-log">
      <span class="lv ${esc(l.level)}">${esc(l.level)}</span> ${esc(l.logger)} · <span class="adm-sub">${ago(l.t)}</span><br>${esc(l.msg)}</div></div>`).join("")
      : `<div class="adm-row"><span class="adm-sub">No warnings since the hub started.</span></div>`}</div>
    <p class="note">Telemetry lives in memory and starts over when the hub restarts. Raw refresh reads each brand directly (at most once every 10 s per brand).</p>`);
}

// Elapsed timers on busy buttons
setInterval(() => {
  if (state.tab !== "admin") return;
  view.querySelectorAll(".elapsed[data-since]").forEach((el) => { el.textContent = `${Math.round((Date.now() - Number(el.dataset.since)) / 1000)} s`; });
}, 1000);

view.addEventListener("change", (e) => {
  if (e.target.id !== "adm-log-brand") return;
  adminLogBrand = e.target.value;
  drawAdmin();
});

function rawResultHtml(r) {
  const label = Object.fromEntries((adminData?.brands || []).map((b) => [b.brand, b.label]));
  const lines = Object.entries(r.brands || {}).map(([b, x]) => x.ok
    ? `${esc(label[b] || b)}: ${x.devices} device${x.devices === 1 ? "" : "s"}, ${fmtMs(x.ms)}`
    : `<b>${esc(label[b] || b)}</b>: ${x.skipped ? "" : "failed, "}${esc(x.error)}`);
  if (r.history) {
    const h = r.history;
    lines.push(h.ok ? `History: ${[h.water_days != null && `${h.water_days} water days`, h.energy_slots != null && `${h.energy_slots} electricity slots`, h.weather_days != null && `${h.weather_days} weather days`].filter(Boolean).join(", ") || "nothing new"}, ${fmtMs(h.ms)}`
      : `<b>History</b>: ${esc(h.error || (h.problems || []).join("; "))}`);
  }
  return `Done in ${fmtMs(r.ms)}<br>` + lines.join("<br>");
}

async function adminAction(key, run) {
  if (adminBusy.has(key)) return;
  adminBusy.set(key, Date.now());
  drawAdmin();
  try {
    adminResult = await run();
  } catch (err) {
    adminResult = { ok: false, html: esc(err.message) };
  } finally {
    adminBusy.delete(key);
  }
  try { adminData = await api("GET", "/api/admin/telemetry"); } catch {}
  if (state.tab === "admin") drawAdmin();
}

view.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-adm]");
  if (!btn || state.tab !== "admin") return;
  const kind = btn.dataset.adm;
  if (kind === "raw" || kind === "backfill" || kind === "brand") {
    const body = kind === "raw" ? { collect: true } : kind === "backfill" ? { backfill_days: 28 } : { brands: [btn.dataset.brand] };
    const key = kind === "brand" ? `brand:${btn.dataset.brand}` : kind;
    adminAction(key, async () => {
      const r = await api("POST", "/api/admin/refresh/raw", body, { timeout: kind === "backfill" ? 600000 : 180000 });
      state.devices = [];  // Rooms reloads with what was just read
      const ok = Object.values(r.brands).every((x) => x.ok) && (!r.history || r.history.ok);
      return { ok, html: rawResultHtml(r) };
    });
  } else if (kind === "cache") {
    const reload = btn.dataset.reload === "1";
    const name = btn.dataset.cache || null;
    adminAction(name ? `cache:${name}:${reload ? 1 : 0}` : `cache-${reload ? "reload" : "clear"}`, async () => {
      const r = await api("POST", "/api/admin/cache/refresh", { cache: name, reload }, { timeout: 120000 });
      const parts = Object.entries(r.caches).map(([n, c]) => `${esc(n)}: ${c.before.size} → ${c.after.size} entries${c.errors.length ? ` (<b>${esc(c.errors.join("; "))}</b>)` : ""}`);
      return { ok: Object.values(r.caches).every((c) => !c.errors.length), html: `${reload ? "Reloaded" : "Cleared"} in ${fmtMs(r.ms)}<br>${parts.join("<br>")}` };
    });
  }
});

// ---------- update the hub: branch, pull, restart ----------
let updatePoll = null;
view.addEventListener("click", (e) => { if (e.target.closest("[data-update-open]")) openUpdate(); });

async function openUpdate() {
  openSheet("Update the hub", `<p class="note">Checking GitHub…</p>`);
  let st;
  try { st = await api("GET", "/api/admin/update"); } catch (err) { $(".sheet-body", sheet).innerHTML = `<p class="note">${esc(err.message)}</p>`; return; }
  drawUpdate(st);
}

function drawUpdate(st) {
  const body = $(".sheet-body", sheet);
  if (!body) return;
  const job = st.job;
  const running = job && (job.state === "running" || job.state === "restarting");
  const opts = st.branches.map((b) => `<option value="${esc(b.name)}" ${b.name === st.branch ? "selected" : ""}>${esc(b.name)} · ${esc(b.updated)}</option>`).join("");
  body.innerHTML = `
    <div class="card">
      <div class="row"><div class="main"><span class="name small">Running ${esc(st.branch)} · ${esc(st.commit)}</span>
        <span class="sub wrap">${esc(st.subject)} (${esc(st.when)})</span></div></div>
      <div class="row"><span class="sub wrap">${st.fetch_error ? `Couldn't check GitHub: ${esc(st.fetch_error)}`
        : st.behind ? `${st.behind} new change(s) on GitHub:` : "Up to date with GitHub."}${st.ahead ? ` This Mac has ${st.ahead} commit(s) GitHub doesn't.` : ""}</span></div>
      ${st.incoming.length ? `<div class="row"><ul class="update-list">${st.incoming.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>` : ""}
      ${st.dirty.length ? `<div class="row"><span class="sub wrap">Edited on this Mac (updating will stop until these are committed or discarded): ${esc(st.dirty.join(", "))}</span></div>` : ""}
    </div>
    <label class="field"><span>Branch</span><select id="upd-branch">${opts}</select></label>
    <div class="inline">
      <button class="pill primary" id="upd-go" ${running ? "disabled" : ""}>Pull and restart</button>
      <button class="pill" id="upd-pull" ${running ? "disabled" : ""}>Pull only</button>
      <button class="pill danger" id="upd-restart" ${running ? "disabled" : ""}>Restart</button>
    </div>
    ${st.service ? "" : `<p class="note">The hub isn't running as the background service, so a restart replaces its process. Run scripts/install-service.sh once to make restarts and start-at-login automatic.</p>`}
    ${job ? `<div class="card update-log"><div class="row"><span class="name small">${{ running: "Updating…", restarting: "Restarting…", done: "Done", failed: "Didn't update" }[job.state] || job.state} · ${esc(job.branch)}</span></div>
      <pre>${esc(job.log.join("\n"))}</pre></div>` : ""}`;
  const start = async (restart) => {
    const branch = $("#upd-branch", sheet).value;
    if (branch !== st.branch && !confirm(`Switch the hub from ${st.branch} to ${branch}?`)) return;
    try { await api("POST", "/api/admin/update", { branch, restart }); pollUpdate(); }
    catch (err) { toast(err.message); }
  };
  $("#upd-go", sheet).addEventListener("click", () => start(true));
  $("#upd-pull", sheet).addEventListener("click", () => start(false));
  $("#upd-restart", sheet).addEventListener("click", async (e) => {
    if (!confirm("Restart the hub now? It's back in about 10 seconds.")) return;
    e.target.disabled = true;
    try { await api("POST", "/api/admin/restart"); toast("Restarting…"); waitForHub(); }
    catch (err) { toast(err.message); e.target.disabled = false; }
  });
  if (running && !updatePoll) pollUpdate();
}

function pollUpdate() {
  clearTimeout(updatePoll);
  updatePoll = setTimeout(async () => {
    updatePoll = null;
    if (!sheet.open) return;
    try {
      const st = await api("GET", "/api/admin/update?fetch=false");
      drawUpdate(st);
      if (st.job?.state === "running") pollUpdate();
      else if (st.job?.state === "restarting") waitForHub();
    } catch { waitForHub(); }      // the hub went away mid-restart
  }, 1500);
}

// After a restart: wait until the hub answers again, then reload so the new code and pages are used.
async function waitForHub() {
  const body = $(".sheet-body", sheet);
  if (body) body.insertAdjacentHTML("beforeend", `<p class="note" id="upd-wait">Waiting for the hub to come back…</p>`);
  await new Promise((r) => setTimeout(r, 4000));
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      if (res.ok) { location.reload(); return; }
    } catch {}
    await new Promise((r) => setTimeout(r, 1500));
  }
  toast("The hub hasn't come back yet. Check the Mac.");
}
