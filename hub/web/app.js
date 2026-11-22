"use strict";

// The old token sign-in is replaced by Google + Face ID; forget any stored token.
try { localStorage.removeItem("hub-token"); } catch {}
const PASSKEY_HINT = "hub-passkey-on-this-device";
const hint = {
  get: () => { try { return localStorage.getItem(PASSKEY_HINT) === "1"; } catch { return false; } },
  set: (on) => { try { on ? localStorage.setItem(PASSKEY_HINT, "1") : localStorage.removeItem(PASSKEY_HINT); } catch {} },
};

// ---------- helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const sheet = $("#sheet");
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const BRAND = { kasa: "Kasa", hue: "Hue", sonos: "Sonos", lg: "LG", govee: "Govee", thinq: "LG ThinQ", dyson: "Dyson", ecovacs: "Ecovacs", pentair: "Pentair", schlage: "Schlage", smt: "Smart Meter Texas", water: "My Water Advisor", rainbird: "Rain Bird", hub: "Hub" };
// These brands have no rename API, so the hub keeps the name.
const HUB_NAMED = new Set(["govee", "lg", "thinq", "dyson", "ecovacs", "pentair", "schlage", "smt", "water", "rainbird"]);

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401 && !path.startsWith("/api/auth/")) { state.signedIn = false; showLogin(); throw new Error("Sign in required"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `Error ${res.status}`);
  }
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2800);
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function setSync(text) { $("#sync").textContent = text; }

// ---------- state ----------
const state = { tab: "rooms", devices: [], rooms: [], scenes: [], schedules: [], errors: [], hue: null, status: null, signedIn: false, me: null, passkeys: [] };

async function loadDevices() {
  const data = await api("GET", "/api/devices");
  state.devices = data.devices;
  state.rooms = data.rooms;
  state.errors = data.errors;
  setSync("Updated " + new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
}

// ---------- tabs ----------
document.querySelectorAll(".tabs button").forEach((btn) =>
  btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#title").textContent = { rooms: "Home", scenes: "Scenes", schedules: "Schedules", insights: "Insights", settings: "Settings" }[tab];
  render(true);
}

async function render(fetch = false) {
  try {
    if (state.tab === "rooms") { if (fetch || !state.devices.length) await loadDevices(); renderRooms(); }
    if (state.tab === "scenes") { if (fetch) state.scenes = await api("GET", "/api/scenes"); renderScenes(); }
    if (state.tab === "schedules") {
      if (fetch) {
        if (!state.devices.length) await loadDevices();
        const data = await api("GET", "/api/schedules");
        state.schedules = data.schedules; state.errors = data.errors;
      }
      renderSchedules();
    }
    if (state.tab === "insights") await renderInsights(fetch || !state.insights);
    if (state.tab === "settings") {
      if (fetch) {
        state.status = await api("GET", "/api/status");
        state.hue = state.status.hue;
        state.passkeys = state.me?.email ? await api("GET", "/api/auth/passkeys") : [];
        await loadDevices();
      }
      renderSettings();
    }
  } catch (err) {
    if (state.signedIn) view.innerHTML = `<div class="error">${esc(err.message)}</div>`;
  }
}

function errorsHtml() {
  return state.errors.map((e) => `<div class="error">${esc(e)}</div>`).join("");
}

// ---------- rooms ----------
function deviceSub(d) {
  const parts = [BRAND[d.brand] || d.brand];
  if (!d.online) parts.push("Not responding");
  else if (d.kind === "speaker") parts.push(d.playing ? (d.media ? `Playing ${d.media}` : "Playing") : "Paused");
  else if (d.kind === "tv") parts.push(d.on ? (d.media ? `On · ${d.media}` : "On") : "Off");
  else if (["appliance", "purifier", "pool", "meter", "sprinkler"].includes(d.kind)) parts.push(d.media || (d.on == null ? "Idle" : d.on ? "On" : "Off"));
  else if (d.kind === "lock") parts.push([d.media, d.battery != null && d.battery <= 25 ? `battery ${d.battery}%` : ""].filter(Boolean).join(" · "));
  else if (d.kind === "vacuum") parts.push([d.media, d.battery != null ? `${d.battery}% battery` : ""].filter(Boolean).join(" · "));
  else if (d.on && d.brightness) parts.push(`${d.brightness}%`);
  else parts.push(d.on ? "On" : "Off");
  return parts.join(" · ");
}

// ---------- icons ----------
const ICON_PATHS = {
  light: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/>',
  plug: '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
  speaker: '<rect x="6" y="3" width="12" height="18" rx="3"/><circle cx="12" cy="14" r="3"/><circle cx="12" cy="7.5" r="1"/>',
  tv: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/>',
  appliance: '<rect x="4" y="3" width="16" height="18" rx="2.5"/><circle cx="12" cy="13" r="4"/><path d="M8 6.5h.01M11 6.5h.01"/>',
  vacuum: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.5"/><path d="M12 3.5v3"/>',
  purifier: '<path d="M12 12c0-4 1-8 4-8s3 4 0 6M12 12c4 0 8 1 8 4s-4 3-6 0M12 12c0 4-1 8-4 8s-3-4 0-6M12 12c-4 0-8-1-8-4s4-3 6 0"/>',
  pool: '<path d="M3 16c2 0 2-1.5 4.5-1.5S10 16 12 16s2-1.5 4.5-1.5S19 16 21 16M3 20c2 0 2-1.5 4.5-1.5S10 20 12 20s2-1.5 4.5-1.5S19 20 21 20M8 13V6a2 2 0 0 1 4 0M14 13V6a2 2 0 0 1 4 0"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  unlock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.6-1.8"/>',
  meter: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  sprinkler: '<path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z"/>',
  power: '<path d="M12 3v8M6.3 7a8 8 0 1 0 11.4 0"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
};
ICON_PATHS.dimmer = ICON_PATHS.light;
function icon(name, cls = "ic") {
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICON_PATHS[name] || ICON_PATHS.power}</svg>`;
}
function deviceIcon(d) {
  if (d.kind === "lock") return icon(d.on ? "lock" : "unlock");
  return icon(d.kind);
}

// A device is "active" when it's on, playing, running or unlocked (unlocked is a warning).
function isActive(d) {
  if (!d.online) return false;
  if (d.kind === "speaker") return !!d.playing;
  if (d.kind === "lock") return false;
  return !!d.on;
}

function deviceTile(d) {
  const active = isActive(d);
  const toggles = d.capabilities.includes("onoff") && d.online;
  const glow = active && d.color ? `style="--glow:${esc(d.color)}"` : "";
  const unlocked = d.kind === "lock" && d.online && !d.on;
  const iconHtml = toggles
    ? `<button class="tile-icon" data-toggle="${esc(d.id)}" aria-label="${esc(d.name)}: turn ${active ? "off" : "on"}" aria-pressed="${active}">${deviceIcon(d)}</button>`
    : `<span class="tile-icon">${deviceIcon(d)}</span>`;
  return `
    <div class="dtile ${active ? "active" : ""} ${d.online ? "" : "offline"} ${unlocked ? "alert" : ""} ${d.color && active ? "tinted" : ""}" ${glow}>
      ${iconHtml}
      <button class="tile-body" data-open="${esc(d.id)}">
        <span class="tile-name">${esc(d.name)}</span>
        <span class="tile-status">${esc(tileStatus(d))}</span>
      </button>
      ${d.kind === "sprinkler" && d.on ? `<button class="pill danger tile-act" data-stop="${esc(d.id)}">Stop</button>` : ""}
      ${unlocked ? `<button class="pill primary tile-act" data-lock="${esc(d.id)}">Lock</button>` : ""}
    </div>`;
}

function tileStatus(d) {
  // deviceSub without the brand prefix: the tile has less room.
  return deviceSub(d).split(" · ").slice(1).join(" · ") || (d.on ? "On" : "Off");
}

function homeSummary() {
  const on = state.devices.filter((d) => d.online && ["light", "dimmer"].includes(d.kind) && d.on).length;
  const playing = state.devices.filter((d) => d.kind === "speaker" && d.playing).map((d) => d.name);
  const unlocked = state.devices.filter((d) => d.kind === "lock" && d.online && !d.on).length;
  const offline = state.devices.filter((d) => !d.online).length;
  const bits = [on ? `${on} light${on > 1 ? "s" : ""} on` : "Lights off"];
  if (playing.length) bits.push(`${playing.join(", ")} playing`);
  if (unlocked) bits.push(`${unlocked} unlocked`);
  if (offline) bits.push(`${offline} not responding`);
  return bits.join(" · ");
}

function renderRooms() {
  const groups = state.rooms.map((r) => ({ room: r, devices: state.devices.filter((d) => d.room_id === r.id) }));
  const unassigned = state.devices.filter((d) => d.room_id == null);
  const section = (title, devices, roomId) => {
    const on = devices.filter(isActive).length;
    const canPower = devices.some((d) => d.capabilities.includes("onoff"));
    return `
      <section class="room">
        <div class="room-head">
          <h2>${esc(title)}</h2>
          <span class="room-count">${on ? `${on} on` : ""}</span>
          ${roomId != null && canPower ? `<span class="room-actions">
            <button class="chip-btn" data-room-power="${roomId}" data-on="true" aria-label="All on in ${esc(title)}">All on</button>
            <button class="chip-btn" data-room-power="${roomId}" data-on="false" aria-label="All off in ${esc(title)}">All off</button>
          </span>` : ""}
        </div>
        <div class="tile-grid">${devices.map(deviceTile).join("")}</div>
      </section>`;
  };
  let html = `<p class="summary">${esc(homeSummary())}</p>`;
  for (const { room, devices } of groups) if (devices.length) html += section(room.name, devices, room.id);
  if (unassigned.length) html += section("No room yet", unassigned, null) + `<p class="note">Tap a device to rename it or put it in a room.</p>`;
  if (!state.devices.length) html = `<p class="empty">Looking for devices… Kasa, Sonos and Govee appear on their own; pair the Hue Bridge and LG TV in Settings.</p>`;
  view.innerHTML = html + errorsHtml();
}

view.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-toggle]");
  if (!btn) return;
  const d = state.devices.find((x) => x.id === btn.dataset.toggle);
  if (!d) return;
  const turnOn = !isActive(d);
  const tile = btn.closest(".dtile");
  tile.classList.toggle("active", turnOn);  // feel instant; corrected below if it fails
  btn.disabled = true;
  try {
    await api("POST", `/api/devices/${encodeURIComponent(d.id)}/state`, { on: turnOn });
    if (d.kind === "speaker") d.playing = turnOn; else d.on = turnOn;
  } catch (err) { toast(err.message); }
  renderRooms();
});

view.addEventListener("change", async (e) => {
  const id = e.target.dataset.power;
  if (!id) return;
  const on = e.target.checked;
  const dev = state.devices.find((d) => d.id === id);
  try {
    await api("POST", `/api/devices/${encodeURIComponent(id)}/state`, { on });
    if (dev) dev.on = on;
    renderRooms();
  } catch (err) { e.target.checked = !on; toast(err.message); }
});

view.addEventListener("click", async (e) => {
  const stopBtn = e.target.closest("[data-stop]");
  if (stopBtn) {
    stopBtn.disabled = true;
    try { await api("POST", `/api/devices/${encodeURIComponent(stopBtn.dataset.stop)}/state`, { action: "stop" }); toast("Stopped"); await loadDevices(); renderRooms(); }
    catch (err) { toast(err.message); stopBtn.disabled = false; }
    return;
  }
  const lockBtn = e.target.closest("[data-lock]");
  if (lockBtn) {
    lockBtn.disabled = true;
    try { await api("POST", `/api/devices/${encodeURIComponent(lockBtn.dataset.lock)}/state`, { locked: true }); toast("Locking…"); await loadDevices(); renderRooms(); }
    catch (err) { toast(err.message); lockBtn.disabled = false; }
    return;
  }
  const open = e.target.closest("[data-open]");
  if (open) return openDevice(open.dataset.open);
  const roomBtn = e.target.closest("[data-room-power]");
  if (roomBtn) {
    roomBtn.disabled = true;
    try {
      const r = await api("POST", `/api/rooms/${roomBtn.dataset.roomPower}/state`, { on: roomBtn.dataset.on === "true" });
      if (!r.ok) toast(`${r.failed.length} device(s) didn't respond`);
      await loadDevices(); renderRooms();
    } catch (err) { toast(err.message); }
    roomBtn.disabled = false;
  }
});

// ---------- device sheet ----------
function openSheet(title, body) {
  sheet.innerHTML = `
    <div class="sheet-head"><h2>${esc(title)}</h2><button class="pill" data-close>Done</button></div>
    <div class="sheet-body">${body}</div>`;
  sheet.showModal();
}
sheet.addEventListener("click", (e) => {
  if (e.target === sheet || e.target.closest("[data-close]")) sheet.close();
});
sheet.addEventListener("close", () => render(state.tab !== "settings"));

function openDevice(id) {
  const d = state.devices.find((x) => x.id === id);
  if (!d) return;
  const roomOptions = [`<option value="">No room</option>`]
    .concat(state.rooms.map((r) => `<option value="${r.id}" ${r.id === d.room_id ? "selected" : ""}>${esc(r.name)}</option>`))
    .join("");
  const caps = new Set(d.capabilities);
  openSheet(d.name, `
    <label class="field"><span>${HUB_NAMED.has(d.brand) ? "Name" : `Name (also changes it in the ${esc(BRAND[d.brand] || d.brand)} app)`}</span>
      <input type="text" id="f-name" value="${esc(d.name)}" maxlength="32"></label>
    <label class="field"><span>Room</span><select id="f-room">${roomOptions}</select></label>
    ${caps.has("playback") && d.on !== false || d.kind === "speaker" ? `<div class="field"><span id="f-media">${esc(d.media || (d.kind === "tv" ? "TV" : "Nothing playing"))}</span>
      <div class="inline media">
        <button class="pill" data-play="previous" aria-label="Previous">⏮</button>
        <button class="pill primary" data-play="${d.playing ? "pause" : "play"}" aria-label="Play or pause">${d.playing ? "⏸" : "▶"}</button>
        <button class="pill" data-play="next" aria-label="Next">⏭</button>
      </div></div>` : ""}
    ${caps.has("volume") && d.volume != null ? `<label class="field"><span>Volume <b id="f-vol-v">${d.volume}</b></span>
      <input type="range" id="f-vol" min="0" max="100" value="${d.volume}"></label>` : ""}
    ${caps.has("mute") && d.muted != null ? `<label class="field inline"><span>Mute</span>
      <span class="switch"><input type="checkbox" id="f-mute" ${d.muted ? "checked" : ""}><span></span></span></label>` : ""}
    ${caps.has("source") && d.sources.length ? `<label class="field"><span>Input or app</span><select id="f-src">
      ${d.source ? "" : `<option value="">Choose…</option>`}${d.sources.map((x) => `<option value="${esc(x.id)}" ${x.id === d.source ? "selected" : ""}>${esc(x.label)}</option>`).join("")}</select></label>` : ""}
    ${caps.has("zones") ? `<div class="field"><span>${esc(d.media || "")}</span>
      <div class="inline"><label class="inline"><span>Run for</span>
        <select id="f-minutes">${[5, 10, 15, 20, 30, 45, 60].map((m) => `<option value="${m}" ${m === 10 ? "selected" : ""}>${m} min</option>`).join("")}</select></label>
        ${d.on ? `<button class="pill danger" id="f-stop">Stop watering</button>` : ""}</div>
      <div class="card">${d.zones.map((z) => `
        <div class="row ${z.running ? "running" : ""}">
          <input type="text" class="zone-name" data-zone-name="${esc(z.id)}" value="${esc(z.name || "")}" placeholder="Zone ${esc(z.id)}" aria-label="Name for zone ${esc(z.id)}" maxlength="24">
          <span class="sub-value">Zone ${esc(z.id)}</span>
          <input type="number" class="zone-area" data-zone-area="${esc(z.id)}" value="${z.area_sqft ?? ""}" min="0" placeholder="sq ft" aria-label="Area of zone ${esc(z.id)} in square feet">
          ${z.schedule ? `<button class="pill" data-zone-sched="${esc(z.id)}" title="${esc(`${z.schedule.minutes} min at ${z.schedule.starts.join(", ") || "no start times"}`)}">Schedule</button>` : ""}
          <button class="pill ${z.running ? "danger" : "primary"}" data-zone="${esc(z.id)}">${z.running ? "Stop" : "Run"}</button>
        </div>`).join("")}</div></div>
      <label class="field"><span>Rain delay</span><select id="f-raindelay">${[0, 1, 2, 3, 5, 7, 14].map((n) => `<option value="${n}" ${n === (d.rain_delay ?? 0) ? "selected" : ""}>${n ? `${n} day${n > 1 ? "s" : ""}` : "Off"}</option>`).join("")}</select></label>
      ${d.details.length ? "" : `<p class="note">The controller's own programs appear here when it reports them.</p>`}
      <div class="field"><span>Smart programs (run by the hub)</span><div id="f-programs"><p class="note">Loading…</p></div></div>` : ""}
    ${caps.has("lock") ? `<div class="field"><span>${esc(d.media || "")}</span><div class="inline">
      <button class="pill primary" id="f-lock" ${d.online ? "" : "disabled"}>Lock</button>
      <button class="pill danger" id="f-unlock" ${d.online ? "" : "disabled"}>Unlock</button></div>
      <p class="note">Unlocking takes a second tap to confirm. Shortcuts and scripts can lock, but never unlock.</p></div>` : ""}
    ${caps.has("actions") && d.actions.length ? `<div class="field"><span>${esc(d.media || "Controls")}</span>
      <div class="inline actions">${d.actions.map((a) => {
        const locked = a.id === "START" && d.ready === false;
        return `<button class="pill ${a.id === "START" || a.id === "clean" || a.id === "resume" ? "primary" : ""}" data-action="${esc(a.id)}" ${locked ? "disabled" : ""}>${esc(a.label)}</button>`;
      }).join("")}</div>
      ${d.ready === false ? `<p class="note">To start it from here, choose the cycle and press <b>Remote Start</b> on the machine. LG requires this so a machine can't be started by accident. Pause and power off always work.</p>` : ""}
      ${d.ready === true ? `<p class="note">Remote Start is on. The cycle chosen on the machine will run.</p>` : ""}</div>` : ""}
    ${d.delay_max ? `<div class="field"><span>Delayed start</span><div class="inline">
      <input type="number" id="f-delay" min="1" max="${d.delay_max}" value="3" aria-label="Hours from now" ${d.ready ? "" : "disabled"}>
      <button class="pill" id="f-delay-go" ${d.ready ? "" : "disabled"}>Start in hours</button></div></div>` : ""}
    ${caps.has("level") ? `<label class="field"><span>Fan speed <b id="f-level-v">${d.level ?? "Auto"}</b></span>
      <input type="range" id="f-level" min="1" max="${d.level_max ?? 10}" value="${d.level ?? 5}"></label>` : ""}
    ${d.details.length ? `<div class="field"><span>${d.kind === "sprinkler" ? "Programs on the controller" : "Readings"}</span><div class="card">${d.details.map((x) => `
      <div class="row"><span class="main"><span class="name small">${esc(x.label)}</span></span><span class="sub-value">${esc(x.value)}</span></div>`).join("")}</div></div>` : ""}
    ${d.battery != null ? `<p class="note">Battery ${d.battery}%</p>` : ""}
    ${caps.has("brightness") ? `<label class="field"><span>Brightness <b id="f-bri-v">${d.brightness ?? 100}%</b></span>
      <input type="range" id="f-bri" min="1" max="100" value="${d.brightness ?? 100}"></label>` : ""}
    ${caps.has("color") ? `<label class="field"><span>Color</span><input type="color" id="f-color" value="${esc(d.color || "#ffffff")}"></label>` : ""}
    ${caps.has("color_temp") ? `<label class="field"><span>White: cool to warm</span>
      <input class="temp-range" type="range" id="f-ct" min="${d.mirek_min ?? 153}" max="${d.mirek_max ?? 500}" value="${d.mirek ?? 366}"></label>` : ""}
    <p class="note">${esc(d.model || "")}</p>`);

  const send = debounce(async (body) => {
    try { await api("POST", `/api/devices/${encodeURIComponent(id)}/state`, body); }
    catch (err) { toast(err.message); }
  }, 250);

  $("#f-name", sheet).addEventListener("change", async (e) => {
    const name = e.target.value.trim();
    if (!name || name === d.name) return;
    try { await api("PATCH", `/api/devices/${encodeURIComponent(id)}`, { name }); d.name = name; toast("Renamed"); }
    catch (err) { toast(err.message); e.target.value = d.name; }
  });
  $("#f-room", sheet).addEventListener("change", async (e) => {
    const room_id = e.target.value ? Number(e.target.value) : null;
    try { await api("PATCH", `/api/devices/${encodeURIComponent(id)}`, { room_id }); d.room_id = room_id; }
    catch (err) { toast(err.message); }
  });
  $("#f-bri", sheet)?.addEventListener("input", (e) => {
    $("#f-bri-v", sheet).textContent = `${e.target.value}%`;
    send({ brightness: Number(e.target.value) });
  });
  $("#f-color", sheet)?.addEventListener("input", (e) => send({ color: e.target.value }));
  $("#f-ct", sheet)?.addEventListener("input", (e) => send({ mirek: Number(e.target.value) }));
  $("#f-vol", sheet)?.addEventListener("input", (e) => {
    $("#f-vol-v", sheet).textContent = e.target.value;
    send({ volume: Number(e.target.value) });
  });
  $("#f-mute", sheet)?.addEventListener("change", (e) => send({ muted: e.target.checked }));
  $("#f-level", sheet)?.addEventListener("input", (e) => {
    $("#f-level-v", sheet).textContent = e.target.value;
    send({ level: Number(e.target.value) });
  });
  const act = async (body, btn, done) => {
    if (btn) btn.disabled = true;
    try { await api("POST", `/api/devices/${encodeURIComponent(id)}/state`, body); toast(done); sheet.close(); }
    catch (err) { toast(err.message); if (btn) btn.disabled = false; }
  };
  $("#f-lock", sheet)?.addEventListener("click", (e) => act({ locked: true }, e.target, "Locking…"));
  $("#f-stop", sheet)?.addEventListener("click", (e) => act({ action: "stop" }, e.target, "Stopped"));
  sheet.querySelectorAll("[data-zone]").forEach((btn) => btn.addEventListener("click", () => {
    const z = d.zones.find((x) => x.id === btn.dataset.zone);
    if (z?.running) return act({ action: "stop" }, btn, "Stopped");
    const minutes = Number($("#f-minutes", sheet).value);
    act({ zone: Number(btn.dataset.zone), minutes }, btn, `Zone ${btn.dataset.zone}: ${minutes} min`);
  }));
  $("#f-raindelay", sheet)?.addEventListener("change", (e) =>
    act({ rain_delay_days: Number(e.target.value) }, null, Number(e.target.value) ? `Rain delay: ${e.target.value} day(s)` : "Rain delay off"));
  sheet.querySelectorAll("[data-zone-area]").forEach((input) => input.addEventListener("change", async () => {
    try { await api("PUT", `/api/rainbird/zones/${input.dataset.zoneArea}`, { area_sqft: Number(input.value) || null }); toast("Zone area saved"); }
    catch (err) { toast(err.message); }
  }));
  sheet.querySelectorAll("[data-zone-sched]").forEach((btn) => btn.addEventListener("click", () =>
    openZoneSchedule(d, d.zones.find((z) => z.id === btn.dataset.zoneSched))));
  if (caps.has("zones")) loadPrograms(d);
  sheet.querySelectorAll("[data-zone-name]").forEach((input) => input.addEventListener("change", async () => {
    try { await api("PUT", `/api/rainbird/zones/${input.dataset.zoneName}`, { name: input.value.trim() }); toast("Zone renamed"); }
    catch (err) { toast(err.message); }
  }));
  const unlock = $("#f-unlock", sheet);
  let armed = null;
  unlock?.addEventListener("click", () => {
    if (!armed) {
      unlock.textContent = "Tap again to unlock";
      armed = setTimeout(() => { armed = null; unlock.textContent = "Unlock"; }, 4000);
      return;
    }
    clearTimeout(armed); armed = null;
    act({ locked: false }, unlock, "Unlocking…");
  });
  sheet.querySelectorAll("[data-action]").forEach((btn) => btn.addEventListener("click",
    () => act({ action: btn.dataset.action }, btn, `${btn.textContent}: sent`)));
  $("#f-delay-go", sheet)?.addEventListener("click", (e) =>
    act({ delay_start_hours: Number($("#f-delay", sheet).value) }, e.target, "Delayed start set"));
  $("#f-src", sheet)?.addEventListener("change", (e) => e.target.value && send({ source: e.target.value }));
  sheet.querySelectorAll("[data-play]").forEach((btn) => btn.addEventListener("click", async () => {
    const playback = btn.dataset.play;
    try {
      await api("POST", `/api/devices/${encodeURIComponent(id)}/state`, { playback });
      if (playback === "play" || playback === "pause") {
        d.playing = playback === "play";
        btn.dataset.play = d.playing ? "pause" : "play";
        btn.textContent = d.playing ? "⏸" : "▶";
      } else {
        // Skipping changes the track: show the new title.
        await loadDevices();
        const fresh = state.devices.find((x) => x.id === id);
        if (fresh?.media) $("#f-media", sheet).textContent = fresh.media;
      }
    } catch (err) { toast(err.message); }
  }));
}

// ---------- scenes ----------
function sceneHue(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function renderScenes() {
  if (!state.scenes.length) {
    view.innerHTML = `<p class="empty">No scenes yet. Hue scenes appear here after you pair the Hue Bridge in Settings.</p>`;
    return;
  }
  const groups = {};
  for (const s of state.scenes) (groups[s.group || "Other"] ||= []).push(s);
  const names = Object.keys(groups);
  if (!state.sceneGroup || !groups[state.sceneGroup]) {
    state.sceneGroup = names.find((g) => groups[g].some((s) => s.active)) || names[0];
  }
  const scenes = groups[state.sceneGroup];
  view.innerHTML = `
    <div class="filter-chips" role="tablist" aria-label="Room">${names.map((g) => `
      <button role="tab" class="fchip ${g === state.sceneGroup ? "on" : ""}" aria-selected="${g === state.sceneGroup}" data-scene-group="${esc(g)}">${esc(g)}${groups[g].some((s) => s.active) ? ' <span class="live-dot" aria-label="a scene is on"></span>' : ""}</button>`).join("")}
    </div>
    <div class="scene-grid">${scenes.map((s) => `
      <div class="stile ${s.active ? "active" : ""}" style="--h:${sceneHue(s.name)}">
        <button class="stile-main" data-scene="${esc(s.id)}">
          <span class="swatch" aria-hidden="true"></span>
          <span class="stile-name">${esc(s.name)}</span>
          ${s.active ? `<span class="stile-state">On</span>` : ""}
        </button>
        ${s.dynamic ? `<button class="stile-dyn" data-scene="${esc(s.id)}" data-dynamic="1" aria-label="${esc(s.name)}, dynamic: colors slowly shift" title="Dynamic: colors slowly shift">${icon("sparkle")}</button>` : ""}
      </div>`).join("")}</div>`;
}

view.addEventListener("click", (e) => {
  const chip = e.target.closest("[data-scene-group]");
  if (!chip) return;
  state.sceneGroup = chip.dataset.sceneGroup;
  renderScenes();
});

view.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-scene]");
  if (!btn) return;
  try {
    await api("POST", `/api/scenes/${encodeURIComponent(btn.dataset.scene)}/activate`, { dynamic: !!btn.dataset.dynamic });
    toast("Scene on");
    state.scenes = await api("GET", "/api/scenes");
    renderScenes();
  } catch (err) { toast(err.message); }
});

// ---------- schedules ----------
function fmtTime(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return new Date(2000, 0, 1, h, m).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtTrigger(t) {
  if (!t) return "";
  if (t.type === "time") return fmtTime(t.time);
  const label = t.type === "sunset" ? "Sunset" : "Sunrise";
  if (!t.offset_min) return label;
  return `${Math.abs(t.offset_min)} min ${t.offset_min < 0 ? "before" : "after"} ${label.toLowerCase()}`;
}
function fmtDays(days) {
  if (days.length === 7) return "Every day";
  if (days.join() === "mon,tue,wed,thu,fri") return "Weekdays";
  if (days.join() === "sat,sun") return "Weekends";
  return days.map((d) => DAY_LABEL[d]).join(" ");
}
function fmtAction(a) {
  if (!a) return "";
  return a.on ? (a.brightness ? `On at ${a.brightness}%` : "Turn on") : "Turn off";
}

function renderSchedules() {
  const rows = state.schedules.map((s) => {
    const what = s.action ? `${fmtAction(s.action)} · ${fmtTrigger(s.trigger)} · ${fmtDays(s.days)}`
      : [s.summary, fmtTrigger(s.trigger), s.days.length ? fmtDays(s.days) : ""].filter(Boolean).join(" · ");
    return `
      <div class="row">
        <div class="main">
          <span class="name">${esc(s.device_names.join(", ") || s.name)}</span>
          <span class="sub">${esc(what)}</span>
          <span class="sub"><span class="badge">${esc(s.runs_on)}</span><span class="badge ${s.origin === "app" ? "app" : ""}">${s.origin === "app" ? `Made in ${esc(BRAND[s.brand] || s.brand)} app` : "Made here"}</span></span>
        </div>
        ${s.can_delete ? `<button class="pill danger" data-del-sched="${esc(s.id)}" aria-label="Delete">Delete</button>` : ""}
        ${s.can_toggle ? `<label class="switch" aria-label="Schedule on or off"><input type="checkbox" data-toggle-sched="${esc(s.id)}" ${s.enabled ? "checked" : ""}><span></span></label>` : ""}
      </div>`;
  }).join("");
  view.innerHTML = `
    <div class="section-title"><span>All schedules</span>
      <span class="actions"><button class="pill primary" id="new-sched">New schedule</button></span></div>
    ${rows ? `<div class="card">${rows}</div>` : `<p class="empty">No schedules yet.</p>`}
    <p class="note">Kasa and Hue schedules are saved on the device, so they run even when the Mac is off. Schedules marked “Hub (this Mac)” run only while the hub is running.</p>
    ${errorsHtml()}`;
  $("#new-sched").addEventListener("click", openNewSchedule);
}

view.addEventListener("change", async (e) => {
  const id = e.target.dataset.toggleSched;
  if (!id) return;
  try { await api("PATCH", `/api/schedules/${encodeURIComponent(id)}`, { enabled: e.target.checked }); }
  catch (err) { e.target.checked = !e.target.checked; toast(err.message); }
});
view.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-del-sched]");
  if (!btn) return;
  if (!confirm("Delete this schedule? It is removed from the device too.")) return;
  try {
    await api("DELETE", `/api/schedules/${encodeURIComponent(btn.dataset.delSched)}`);
    toast("Deleted"); render(true);
  } catch (err) { toast(err.message); }
});

function openNewSchedule() {
  // Kasa and Hue keep schedules on the device; other on/off devices are scheduled by the hub.
  const devices = state.devices.filter((d) => d.online && d.kind !== "lock"
    && (d.capabilities.includes("schedule") || d.capabilities.includes("onoff")));
  openSheet("New schedule", `
    <label class="field"><span>Name</span><input type="text" id="s-name" value="Schedule" maxlength="32"></label>
    <div class="field"><span>Devices</span><div class="chips">${devices.map((d) => `
      <label class="chip"><input type="checkbox" name="s-dev" value="${esc(d.id)}"><span>${esc(d.name)}</span></label>`).join("")}</div></div>
    <div class="field"><span>Action</span><div class="inline">
      <select id="s-on"><option value="1">Turn on</option><option value="0">Turn off</option></select>
      <input type="number" id="s-bri" min="1" max="100" placeholder="Brightness % (optional)"></div></div>
    <div class="field"><span>When</span><div class="inline">
      <select id="s-type"><option value="time">At a time</option><option value="sunset">Sunset</option><option value="sunrise">Sunrise</option></select>
      <input type="time" id="s-time" value="07:00">
      <input type="number" id="s-offset" value="0" min="-180" max="180" title="Minutes after (negative for before)" hidden></div></div>
    <div class="field"><span>Days</span><div class="chips">${DAYS.map((d) => `
      <label class="chip"><input type="checkbox" name="s-day" value="${d}" checked><span>${DAY_LABEL[d]}</span></label>`).join("")}</div></div>
    <p class="note">Kasa plugs support times, sunrise and sunset. The Hue Bridge supports set times. Other devices (pool pump, Govee, Dyson, Sonos…) use set times and run in the hub, so the Mac must be awake. Locks can't be scheduled.</p>
    <button class="pill primary" id="s-save">Save schedule</button>`);

  const type = $("#s-type", sheet);
  type.addEventListener("change", () => {
    $("#s-time", sheet).hidden = type.value !== "time";
    $("#s-offset", sheet).hidden = type.value === "time";
  });
  $("#s-save", sheet).addEventListener("click", async (e) => {
    const checked = (name) => [...sheet.querySelectorAll(`input[name=${name}]:checked`)].map((i) => i.value);
    const on = $("#s-on", sheet).value === "1";
    const bri = Number($("#s-bri", sheet).value) || null;
    const body = {
      name: $("#s-name", sheet).value.trim() || "Schedule",
      device_ids: checked("s-dev"),
      action: { on, brightness: on ? bri : null },
      trigger: type.value === "time"
        ? { type: "time", time: $("#s-time", sheet).value }
        : { type: type.value, offset_min: Number($("#s-offset", sheet).value) || 0 },
      days: checked("s-day"),
      enabled: true,
    };
    if (!body.device_ids.length) return toast("Pick at least one device");
    if (!body.days.length) return toast("Pick at least one day");
    e.target.disabled = true;
    try {
      const r = await api("POST", "/api/schedules", body);
      toast(`Saved on ${r.created.length} device(s)`);
      sheet.close();
    } catch (err) { toast(err.message); e.target.disabled = false; }
  });
}

// ---------- settings ----------
function renderSettings() {
  const hue = state.hue || {};
  const lg = state.status?.lg || {};
  const count = (brand) => state.status?.[brand]?.devices ?? 0;
  const govee = state.status?.govee || {};
  const dyson = state.status?.dyson || {};
  const ecovacs = state.status?.ecovacs || {};
  const pentair = state.status?.pentair || {};
  const schlage = state.status?.schlage || {};
  const smt = state.status?.smt || {};
  const water = state.status?.water || {};
  const rainbird = state.status?.rainbird || {};
  const accountCard = (name, st, form, about, connectedText, key = name.toLowerCase(), extra = "") => `
    <div class="section-title"><span>${esc(name)}</span></div>
    <div class="card">${st.available === false ? `
      <div class="row"><div class="main"><span class="name">Not available</span>
        <span class="sub wrap">${name === "Ecovacs" ? "Needs Python 3.13 on the hub Mac." : "Its library isn't installed."} See the README.</span></div></div>`
      : st.connected ? `
      <div class="row"><div class="main"><span class="name">Connected</span><span class="sub wrap">${connectedText}</span></div>
        ${extra}<button class="pill danger" id="${key}-off">Disconnect</button></div>` : `
      <div class="row"><div class="main"><span class="name">Not connected</span><span class="sub wrap">${about}</span></div></div>${form}`}
    </div>`;
  const thinq = state.status?.thinq || {};
  const fmtDate = (s) => s ? new Date(s * 1000).toLocaleDateString() : "never";
  const keys = state.passkeys.map((p) => `
      <div class="row"><div class="main">
        <span class="name">${esc(p.label)}</span>
        <span class="sub">Added ${esc(fmtDate(p.created))} · last used ${esc(fmtDate(p.last_used))}</span></div>
        <button class="pill danger" data-del-passkey="${esc(p.id)}">Remove</button></div>`).join("");
  const account = state.me?.email ? `
    <div class="section-title"><span>Account</span></div>
    <div class="card">
      <div class="row"><div class="main"><span class="name">${esc(state.me.email)}</span>
        <span class="sub">Signed in with ${state.me.method === "passkey" ? "Face ID" : "Google"}</span></div>
        <button class="pill" id="sign-out">Sign out</button></div>
      ${keys}
      <div class="row"><div class="main"><span class="name">Face ID on this device</span>
        <span class="sub">${hint.get() ? "Turned on" : "Sign in with one tap next time"}</span></div>
        ${hint.get() ? "" : `<button class="pill primary" id="add-passkey">Turn on</button>`}</div>
    </div>` : "";
  view.innerHTML = account + `
    <div class="section-title"><span>Philips Hue</span></div>
    <div class="card"><div class="row"><div class="main">
      <span class="name">${hue.paired ? "Hue Bridge paired" : "Hue Bridge not paired"}</span>
      <span class="sub">${hue.paired ? "Lights, scenes and schedules are available." : "Press the round button on the bridge, then tap Pair."}</span>
    </div><button class="pill ${hue.paired ? "" : "primary"}" id="hue-pair">${hue.paired ? "Pair again" : "Pair"}</button></div></div>

    <div class="section-title"><span>LG TV</span></div>
    <div class="card"><div class="row"><div class="main">
      <span class="name">${lg.paired ? `Connected${lg.model ? ` (${esc(lg.model)})` : ""}` : "Not connected"}</span>
      <span class="sub wrap">${lg.available === false ? "Needs Python 3.12 on the hub Mac. See the README."
        : lg.paired ? (lg.wake_on_lan ? "The hub can turn the TV on and off." : "The hub can turn the TV off. To turn it on too, connect again while the TV is on.")
        : "Turn the TV on, tap Connect, then choose Accept on the TV with the remote within 30 seconds."}</span>
    </div>${lg.available === false ? "" : `<button class="pill ${lg.paired ? "" : "primary"}" id="lg-pair">${lg.paired ? "Connect again" : "Connect"}</button>`}</div></div>

    <div class="section-title"><span>Govee account</span></div>
    <div class="card">${govee.account ? `
      <div class="row"><div class="main"><span class="name">Connected</span>
        <span class="sub">${count("govee")} Govee device(s), ${govee.lan ?? 0} controlled over the local network</span></div>
        <button class="pill danger" id="govee-off">Disconnect</button></div>` : `
      <div class="row"><div class="main"><span class="name">Not connected</span>
        <span class="sub wrap">Adds Govee devices without LAN Control, plugs, and the names from the Govee app. In the Govee Home app: <b>Profile → Settings (gear) → Apply for API Key</b>. The key arrives by email.</span></div></div>
      <div class="row key-row"><input type="password" id="govee-key" placeholder="Govee API key" autocomplete="off" spellcheck="false">
        <button class="pill primary" id="govee-on">Connect</button></div>`}
    </div>

    <div class="section-title"><span>LG ThinQ appliances</span></div>
    <div class="card">${thinq.connected ? `
      <div class="row"><div class="main"><span class="name">Connected (${esc(thinq.country)})</span>
        <span class="sub wrap">${count("thinq")} appliance(s). Washers and dryers start only after Remote Start is pressed on the machine.</span></div>
        <button class="pill danger" id="thinq-off">Disconnect</button></div>` : `
      <div class="row"><div class="main"><span class="name">Not connected</span>
        <span class="sub wrap">Sign in at <b>connect-pat.lgthinq.com</b> with your LG account, choose <b>Add new token</b>, tick every permission, and paste the token here.</span></div></div>
      <div class="row key-row"><input type="password" id="thinq-token" placeholder="Personal access token" autocomplete="off" spellcheck="false">
        <input type="text" id="thinq-country" value="US" maxlength="2" aria-label="Country code" class="country">
        <button class="pill primary" id="thinq-on">Connect</button></div>`}
    </div>

    ${accountCard("Dyson", dyson, dyson.waiting_for_code ? `
      <div class="row key-row"><input type="text" inputmode="numeric" id="dyson-code" placeholder="Code from Dyson's email" autocomplete="one-time-code">
        <input type="password" id="dyson-pw" placeholder="MyDyson password" autocomplete="off">
        <button class="pill primary" id="dyson-verify">Connect</button></div>` : `
      ${dysonStickerForm()}
      <div class="row"><span class="sub wrap">Or, if your Dyson account has a password: enter its email and Dyson emails you a code.</span></div>
      <div class="row key-row"><input type="email" id="dyson-email" placeholder="MyDyson email" autocomplete="off">
        <input type="text" id="dyson-country" class="country" value="US" maxlength="2" aria-label="Country code">
        <button class="pill" id="dyson-send">Email me a code</button></div>`,
      "Purifiers and fans, controlled directly on your Wi‑Fi. If you sign in to Dyson with Google or Apple you have no password: use the sticker on the device (its Wi‑Fi name and password). Nothing from your Dyson account is needed or stored.",
      `${dyson.online ?? 0} of ${count("dyson")} on the network`, "dyson",
      `<button class="pill" id="dyson-add">Add device</button>`)}
    ${accountCard("Ecovacs", ecovacs, `
      <div class="row key-row"><input type="email" id="ecovacs-email" placeholder="Ecovacs email" autocomplete="off">
        <input type="password" id="ecovacs-pw" placeholder="Password" autocomplete="off">
        <input type="text" id="ecovacs-country" class="country" value="US" maxlength="2" aria-label="Country code">
        <button class="pill primary" id="ecovacs-on">Connect</button></div>`,
      "Deebot robot vacuums: clean, pause, return to the dock, battery. The hub keeps a hash of the password, not the password.",
      `${count("ecovacs")} robot(s)`)}
    ${accountCard("Schlage", schlage, `
      <div class="row key-row"><input type="email" id="schlage-email" placeholder="Schlage Home email" autocomplete="off">
        <input type="password" id="schlage-pw" placeholder="Password" autocomplete="off">
        <button class="pill primary" id="schlage-on">Connect</button></div>`,
      "Encode Wi‑Fi locks: lock, unlock, who used it last, battery. Schlage needs the password kept on the hub Mac, so share your lock with a second Schlage account in the Schlage Home app and use that one here.",
      `${count("schlage")} lock(s)`)}
    ${accountCard("Pentair", pentair, `
      <div class="row key-row"><input type="email" id="pentair-email" placeholder="Pentair Home email" autocomplete="off">
        <input type="password" id="pentair-pw" placeholder="Password" autocomplete="off">
        <button class="pill primary" id="pentair-on">Connect</button></div>`,
      "Pool equipment from the Pentair Home app: readings, and turning IntelliFlo pumps on and off.",
      `${count("pentair")} device(s). Pumps turn on and off; schedules for them run in the hub.`, "pentair",
      `<button class="pill" id="pentair-raw">Pump data</button>`)}
    ${accountCard("Rain Bird", rainbird, `
      <div class="row key-row"><input type="text" inputmode="decimal" id="rainbird-host" placeholder="Controller IP, e.g. 192.168.1.40" autocomplete="off">
        <input type="password" id="rainbird-pw" placeholder="Controller password" autocomplete="off">
        <button class="pill primary" id="rainbird-on">Connect</button></div>`,
      "Sprinklers with the Rain Bird LNK Wi‑Fi module, controlled directly on your Wi‑Fi. Use the controller password from the Rain Bird app (not your account password). Find the IP in the Rain Bird app's controller settings or your router's device list.",
      `${esc(rainbird.host || "")} · ${rainbird.zones ?? 0} zone(s)`, "rainbird")}
    <div class="section-title"><span>Photo analysis (optional)</span></div>
    <div class="card">${state.status?.claude?.connected ? `
      <div class="row"><div class="main"><span class="name">Claude is on</span>
        <span class="sub wrap">Zone photos are also sent to Claude (Anthropic) to look for signs of too much or too little water. Each photo costs a few cents of API use.</span></div>
        <button class="pill danger" id="claude-off">Turn off</button></div>` : `
      <div class="row"><div class="main"><span class="name">Greenness only</span>
        <span class="sub wrap">The hub measures how green each zone photo is, on the Mac. To also have Claude assess photos, paste an Anthropic API key from <b>console.anthropic.com → API keys</b>. Photos are then sent to Anthropic.</span></div></div>
      <div class="row key-row"><input type="password" id="claude-key" placeholder="sk-ant-…" autocomplete="off" spellcheck="false">
        <button class="pill primary" id="claude-on">Turn on</button></div>`}
    </div>
    ${accountCard("Smart Meter Texas", smt, `
      <div class="row key-row"><input type="text" id="smt-user" placeholder="Smart Meter Texas username" autocomplete="off">
        <input type="password" id="smt-pw" placeholder="Password" autocomplete="off">
        <button class="pill primary" id="smt-on">Connect</button></div>`,
      "TXU electricity usage, from the state's smart meter portal that TXU reads. Make a free account at <b>smartmetertexas.com</b> (it's separate from your TXU login). The hub reads the meter hourly and yesterday's usage daily.",
      `${count("smt")} meter(s)`, "smt")}
    ${accountCard("McKinney water", water, `
      <div class="row key-row"><input type="email" id="water-user" placeholder="My Water Advisor email" autocomplete="off">
        <input type="password" id="water-pw" placeholder="Password" autocomplete="off">
        <button class="pill primary" id="water-on">Connect</button></div>`,
      "Daily water use from <b>My Water Advisor 2</b>, the City of McKinney's meter portal. Read only.",
      `${count("water")} meter(s)`, "water")}

    <div class="section-title"><span>Found automatically</span></div>
    <div class="card">
      <div class="row"><div class="main"><span class="name">Kasa</span><span class="sub">${count("kasa")} plug(s) and dimmer(s)</span></div></div>
      <div class="row"><div class="main"><span class="name">Sonos</span><span class="sub">${count("sonos")} speaker(s)</span></div></div>
      ${govee.account ? "" : `<div class="row"><div class="main"><span class="name">Govee</span><span class="sub wrap">${count("govee")} light(s). Without the account, a light only shows up after you turn on <b>LAN Control</b> for it in the Govee app.</span></div></div>`}
    </div>

    <div class="section-title"><span>Backup to your S3 bucket</span></div>
    <div class="card" id="backup-card"><div class="row"><span class="sub">Loading…</span></div></div>

    <div class="section-title"><span>Stored on this Mac</span></div>
    <div class="card" id="storage-card"><div class="row"><span class="sub">Loading…</span></div></div>

    <div class="section-title"><span>Rooms</span></div>
    <div class="card">${state.rooms.map((r) => `
      <div class="row">
        <div class="main"><span class="name">${esc(r.name)}</span>
          <span class="sub">${state.devices.filter((d) => d.room_id === r.id).length} device(s)</span></div>
        <button class="pill" data-rename-room="${r.id}">Rename</button>
        <button class="pill danger" data-del-room="${r.id}">Delete</button>
      </div>`).join("") || `<div class="row"><span class="sub">No rooms yet.</span></div>`}
    </div>
    <div class="field inline"><input type="text" id="new-room" placeholder="New room name" maxlength="40">
      <button class="pill primary" id="add-room" style="flex:none">Add room</button></div>
    <p class="note">Rooms live in this hub. Hue rooms are imported the first time the bridge is paired.</p>`;

  loadStorage();
  loadBackup();
  $("#sign-out")?.addEventListener("click", signOut);
  $("#add-passkey")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    if (await registerPasskey()) render(true); else e.target.disabled = false;
  });
  $("#hue-pair").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api("POST", "/api/hue/pair"); toast("Hue Bridge paired"); render(true); }
    catch (err) { toast(err.message); e.target.disabled = false; }
  });
  const accountButton = (btnId, run, done) => $(btnId)?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { const r = await run(); toast(done(r)); render(true); }
    catch (err) { toast(err.message); e.target.disabled = false; }
  });
  accountButton("#govee-on", () => api("POST", "/api/govee/account", { api_key: $("#govee-key").value.trim() }),
    (r) => `Govee connected: ${r.devices} device(s)`);
  accountButton("#thinq-on", () => api("POST", "/api/thinq/account", { token: $("#thinq-token").value.trim(), country: $("#thinq-country").value.trim().toUpperCase() || "US" }),
    (r) => `LG ThinQ connected: ${r.devices} appliance(s)`);
  const val = (sel) => $(sel)?.value.trim();
  bindDysonSticker(view, () => render(true));
  $("#dyson-add")?.addEventListener("click", () => {
    openSheet("Add a Dyson", `<p class="note">Find the sticker on the device or in its manual. The Wi‑Fi name starts with DYSON-. Your Dyson account isn't needed.</p>${dysonStickerForm()}`);
    bindDysonSticker(sheet, () => { sheet.close(); });
  });
  accountButton("#dyson-send", () => api("POST", "/api/dyson/code", { email: val("#dyson-email"), country: (val("#dyson-country") || "US").toUpperCase() }),
    () => "Check your email for Dyson's code");
  accountButton("#dyson-verify", () => api("POST", "/api/dyson/account", { code: val("#dyson-code"), password: $("#dyson-pw").value }),
    (r) => `Dyson connected: ${r.devices} device(s)`);
  accountButton("#ecovacs-on", () => api("POST", "/api/ecovacs/account", { email: val("#ecovacs-email"), password: $("#ecovacs-pw").value, country: (val("#ecovacs-country") || "US").toUpperCase() }),
    (r) => `Ecovacs connected: ${r.devices} robot(s)`);
  accountButton("#pentair-on", () => api("POST", "/api/pentair/account", { email: val("#pentair-email"), password: $("#pentair-pw").value }),
    (r) => `Pentair connected: ${r.devices} device(s)`);
  accountButton("#schlage-on", () => api("POST", "/api/schlage/account", { email: val("#schlage-email"), password: $("#schlage-pw").value }),
    (r) => `Schlage connected: ${r.devices} lock(s)`);
  accountButton("#smt-on", () => api("POST", "/api/smt/account", { username: val("#smt-user"), password: $("#smt-pw").value }),
    (r) => `Smart Meter Texas connected: ${r.devices} meter(s)`);
  accountButton("#water-on", () => api("POST", "/api/water/account", { username: val("#water-user"), password: $("#water-pw").value }),
    (r) => `My Water Advisor connected: ${r.devices} meter(s)`);
  $("#pentair-raw")?.addEventListener("click", async () => {
    try {
      const pumps = await api("GET", "/api/pentair/fields");
      openSheet("Pentair data", pumps.map((p) => `
        <div class="field"><span>${esc(p.name || p.deviceType)} (${esc(p.deviceType)})</span><div class="card">${Object.entries(p.fields).map(([k, f]) => `
          <div class="row"><span class="main"><span class="name small">${esc(k)} · ${esc(f?.name ?? "")}</span></span>
          <span class="sub-value">${esc(f?.value ?? f)} ${esc(f?.unit ?? "")}</span></div>`).join("")}</div></div>`).join("")
        + `<p class="note">Secret fields (security key, Wi‑Fi details) are left out. Sharing this helps work out how the Pentair app stores the pump's own schedules.</p>`);
    } catch (err) { toast(err.message); }
  });
  accountButton("#rainbird-on", () => api("POST", "/api/rainbird/account", { host: val("#rainbird-host"), password: $("#rainbird-pw").value }),
    (r) => `Rain Bird connected: ${r.zones} zone(s)`);
  accountButton("#claude-on", () => api("PUT", "/api/settings/claude", { api_key: val("#claude-key") }), () => "Claude photo analysis is on");
  accountButton("#claude-off", () => api("DELETE", "/api/settings/claude"), () => "Claude photo analysis is off");
  for (const b of ["dyson", "ecovacs", "pentair", "schlage", "smt", "water", "rainbird"]) accountButton(`#${b}-off`, () => api("DELETE", `/api/${b}/account`), () => `${BRAND[b]} disconnected`);
  accountButton("#govee-off", () => api("DELETE", "/api/govee/account"), () => "Govee account disconnected");
  accountButton("#thinq-off", () => api("DELETE", "/api/thinq/account"), () => "LG ThinQ disconnected");
  $("#lg-pair")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Accept on the TV…";
    try {
      const r = await api("POST", "/api/lgtv/pair", {});
      toast(r.wake_on_lan ? "LG TV connected" : "LG TV connected (turning it on needs another Connect while it's on)");
      render(true);
    } catch (err) { toast(err.message); e.target.disabled = false; e.target.textContent = "Connect"; }
  });
  $("#add-room").addEventListener("click", async () => {
    const name = $("#new-room").value.trim();
    if (!name) return;
    try { await api("POST", "/api/rooms", { name }); render(true); } catch (err) { toast(err.message); }
  });
}

view.addEventListener("click", async (e) => {
  const ren = e.target.closest("[data-rename-room]");
  const del = e.target.closest("[data-del-room]");
  const delKey = e.target.closest("[data-del-passkey]");
  try {
    if (delKey && confirm("Remove this Face ID passkey? You can sign in with Google and add it again.")) {
      await api("DELETE", `/api/auth/passkeys/${encodeURIComponent(delKey.dataset.delPasskey)}`);
      if (state.passkeys.length <= 1) hint.set(false);
      render(true);
    }
    if (ren) {
      const room = state.rooms.find((r) => r.id === Number(ren.dataset.renameRoom));
      const name = prompt("Room name", room?.name || "");
      if (name && name.trim()) { await api("PATCH", `/api/rooms/${ren.dataset.renameRoom}`, { name: name.trim() }); render(true); }
    }
    if (del && confirm("Delete this room? Its devices move to “No room yet”.")) {
      await api("DELETE", `/api/rooms/${del.dataset.delRoom}`); render(true);
    }
  } catch (err) { toast(err.message); }
});

// ---------- sign-in: Google + Face ID (passkeys) ----------
const b64ToBuf = (s) => {
  const b = s.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(b + "=".repeat((4 - (b.length % 4)) % 4)), (c) => c.charCodeAt(0)).buffer;
};
const bufToB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const canPasskey = () => !!window.PublicKeyCredential;

// Safari only allows Face ID straight from a tap, so challenges are fetched ahead of time.
let loginChallenge = null;
async function prefetchLoginChallenge() {
  try { loginChallenge = await api("POST", "/api/auth/passkey/login/options"); } catch { loginChallenge = null; }
}

async function passkeySignIn() {
  if (!loginChallenge) await prefetchLoginChallenge();
  const { challenge_id, options } = loginChallenge || {};
  loginChallenge = null;
  if (!options) return toast("Couldn't reach the hub");
  try {
    const cred = await navigator.credentials.get({
      publicKey: {
        ...options,
        challenge: b64ToBuf(options.challenge),
        allowCredentials: (options.allowCredentials || []).map((c) => ({ ...c, id: b64ToBuf(c.id) })),
      },
    });
    const r = cred.response;
    await api("POST", "/api/auth/passkey/login", {
      challenge_id,
      credential: {
        id: cred.id, rawId: bufToB64(cred.rawId), type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment,
        clientExtensionResults: cred.getClientExtensionResults(),
        response: {
          clientDataJSON: bufToB64(r.clientDataJSON), authenticatorData: bufToB64(r.authenticatorData),
          signature: bufToB64(r.signature), userHandle: r.userHandle ? bufToB64(r.userHandle) : null,
        },
      },
    });
    hint.set(true);
    await afterSignIn(false);
  } catch (err) {
    prefetchLoginChallenge();
    if (err.name !== "NotAllowedError") toast(err.message);
  }
}

let registerChallenge = null;
async function registerPasskey() {
  const prepared = registerChallenge || await api("POST", "/api/auth/passkey/register/options");
  registerChallenge = null;
  const { challenge_id, options } = prepared;
  try {
    const cred = await navigator.credentials.create({
      publicKey: {
        ...options,
        challenge: b64ToBuf(options.challenge),
        user: { ...options.user, id: b64ToBuf(options.user.id) },
        excludeCredentials: (options.excludeCredentials || []).map((c) => ({ ...c, id: b64ToBuf(c.id) })),
      },
    });
    const r = cred.response;
    const label = /iPhone|iPad/.test(navigator.userAgent) ? "iPhone" : /Mac/.test(navigator.userAgent) ? "Mac" : "This device";
    await api("POST", "/api/auth/passkey/register", {
      challenge_id, label,
      credential: {
        id: cred.id, rawId: bufToB64(cred.rawId), type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment,
        clientExtensionResults: cred.getClientExtensionResults(),
        response: {
          clientDataJSON: bufToB64(r.clientDataJSON), attestationObject: bufToB64(r.attestationObject),
          transports: r.getTransports ? r.getTransports() : [],
        },
      },
    });
    hint.set(true);
    toast("Face ID is on for this device");
    return true;
  } catch (err) {
    if (err.name === "InvalidStateError") { hint.set(true); toast("Face ID is already on for this device"); return true; }
    if (err.name !== "NotAllowedError") toast(err.message);
    return false;
  }
}

async function googleSignIn() {
  try { location.href = (await api("POST", "/api/auth/google/start")).url; }
  catch (err) { toast(err.message); }
}

// Google sends the user back to "/" with the result in the URL fragment.
async function finishGoogleRedirect() {
  const params = new URLSearchParams(location.hash.slice(1));
  if (!params.has("id_token") && !params.has("error")) return false;
  history.replaceState(null, "", location.pathname);
  if (params.has("error")) { toast("Google sign-in was cancelled"); return false; }
  try {
    await api("POST", "/api/auth/google", { id_token: params.get("id_token"), state: params.get("state") });
    await afterSignIn(true);
    return true;
  } catch (err) { showLogin(); toast(err.message); return false; }
}

// The iPhone app opens /?app_challenge=… in a sign-in sheet; after signing in here,
// a tap hands it a one-time code through its smarthome:// link.
const APP_KEY = "hub-app-challenge";
(() => {
  const c = new URLSearchParams(location.search).get("app_challenge");
  if (c) { try { sessionStorage.setItem(APP_KEY, c); } catch {} history.replaceState(null, "", "/"); }
})();
function offerAppConnect() {
  let challenge = null;
  try { challenge = sessionStorage.getItem(APP_KEY); } catch {}
  if (!challenge) return false;
  openSheet("Connect the iPhone app", `
    <p>Let the Smart Home app on this iPhone control your home? It will ask for Face ID each time it opens.</p>
    <button class="pill primary big" id="app-connect">Connect the iPhone app</button>`);
  $("#app-connect", sheet).addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("POST", "/api/auth/app/code", { code_challenge: challenge });
      try { sessionStorage.removeItem(APP_KEY); } catch {}
      location.href = r.redirect;
    } catch (err) { toast(err.message); e.target.disabled = false; }
  });
  return true;
}

async function afterSignIn(offerPasskey) {
  state.me = await api("GET", "/api/auth/me");
  state.signedIn = state.me.signed_in;
  document.querySelector(".tabs").hidden = false;
  switchTab(state.tab);
  if (offerAppConnect()) return;
  if (offerPasskey && canPasskey() && !hint.get()) offerFaceId();
}

async function offerFaceId() {
  try {
    if (!(await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable())) return;
    registerChallenge = await api("POST", "/api/auth/passkey/register/options");
  } catch { return; }
  openSheet("Use Face ID next time?", `
    <p>Sign in with one tap instead of Google. Your face or fingerprint never leaves this device.</p>
    <div class="inline"><button class="pill" data-close>Not now</button>
      <button class="pill primary" id="faceid-on">Turn on Face ID</button></div>`);
  $("#faceid-on", sheet).addEventListener("click", async () => {
    if (await registerPasskey()) sheet.close();
  });
}

async function signOut() {
  try { await api("POST", "/api/auth/logout"); } catch {}
  state.signedIn = false; state.me = null;
  showLogin();
}

function showLogin() {
  document.querySelector(".tabs").hidden = true;
  $("#title").textContent = "Home";
  setSync("");
  const faceId = canPasskey() && hint.get();
  view.innerHTML = `
    <div class="login">
      <p class="note">Sign in to control your home.</p>
      ${faceId ? `<button class="pill primary big" id="login-passkey">Sign in with Face ID</button>` : ""}
      <button class="pill ${faceId ? "" : "primary"} big" id="login-google">Sign in with Google</button>
      ${canPasskey() && !faceId ? `<button class="pill big" id="login-passkey">Use a passkey</button>` : ""}
    </div>`;
  $("#login-google").addEventListener("click", googleSignIn);
  $("#login-passkey")?.addEventListener("click", passkeySignIn);
  if (canPasskey()) prefetchLoginChallenge();
}

// ---------- start ----------
async function boot() {
  if (await finishGoogleRedirect()) return;
  try {
    state.me = await api("GET", "/api/auth/me");
    state.signedIn = state.me.signed_in;
  } catch { state.signedIn = false; }
  if (state.signedIn) { await afterSignIn(false); } else { showLogin(); }
}
boot();

// Keep the Rooms view in step with changes made in brand apps, wall switches or voice.
setInterval(() => {
  if (document.hidden || sheet.open || state.tab !== "rooms" || !state.signedIn) return;
  loadDevices().then(renderRooms).catch(() => {});
}, 5000);


// ---------- sprinklers: smart programs and zone schedules ----------
const dysonStickerForm = () => `
  <div class="row key-row"><input type="text" id="dyson-ssid" placeholder="Wi‑Fi name, e.g. DYSON-AB1-US-ABC1234A-438" autocomplete="off" autocapitalize="characters" spellcheck="false">
    <input type="text" id="dyson-wifipw" placeholder="Wi‑Fi password from the sticker" autocomplete="off" spellcheck="false">
    <input type="text" id="dyson-name" placeholder="Name, e.g. Bedroom purifier" maxlength="32">
    <button class="pill primary" id="dyson-sticker">Add from sticker</button></div>`;
function bindDysonSticker(root, done) {
  $("#dyson-sticker", root)?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("POST", "/api/dyson/sticker", { ssid: $("#dyson-ssid", root).value.trim(), wifi_password: $("#dyson-wifipw", root).value.trim(), name: $("#dyson-name", root).value.trim() });
      toast(`Added ${r.name}. It appears in Rooms once the hub finds it on your Wi‑Fi.`); done();
    } catch (err) { toast(err.message); e.target.disabled = false; }
  });
}

const dayChips = (name, chosen) => `<div class="chips">${DAYS.map((x) => `
  <label class="chip"><input type="checkbox" name="${name}" value="${x}" ${chosen.includes(x) ? "checked" : ""}><span>${DAY_LABEL[x]}</span></label>`).join("")}</div>`;
const checkedDays = (name) => [...sheet.querySelectorAll(`input[name=${name}]:checked`)].map((i) => i.value);

async function loadPrograms(device) {
  const box = $("#f-programs", sheet);
  try {
    const { programs, decisions } = await api("GET", "/api/irrigation/programs");
    if (!box.isConnected) return;
    const zoneName = (id) => device.zones.find((z) => z.id === String(id))?.label || `Zone ${id}`;
    box.innerHTML = `${programs.length ? `<div class="card">${programs.map((p) => `
      <div class="row prog"><div class="main"><span class="name">${esc(p.name)}</span>
        <span class="sub wrap" title="${esc(p.zones.filter((z) => z.minutes).map((z) => `${zoneName(z.zone)} ${z.minutes} min`).join(", "))}">${esc(p.starts.map(fmtTime).join(", "))} · ${esc(fmtDays(p.days))} · ${p.zones.filter((z) => z.minutes).length} zones, ${p.zones.reduce((a, z) => a + z.minutes, 0)} min${p.smart ? " · adjusts for weather" : ""}</span></div>
        <button class="pill" data-prog-run="${p.id}">Run now</button>
        <button class="pill" data-prog-edit="${p.id}">Edit</button>
        <label class="switch" aria-label="Program on or off"><input type="checkbox" data-prog-toggle="${p.id}" ${p.enabled ? "checked" : ""}><span></span></label>
      </div>`).join("")}</div>` : `<p class="note">None yet. A smart program skips watering after rain and shortens it in cool weather. If you use one, turn the controller's own programs off in the Rain Bird app so the lawn isn't watered twice.</p>`}
      <button class="pill primary" id="prog-new">New smart program</button>
      ${decisions.length ? `<p class="note">Recent: ${decisions.slice(0, 3).map((x) => `${esc(x.at.replace("T", " "))}: ${esc(x.reason)}`).join("<br>")}</p>` : ""}`;
    $("#prog-new", sheet).addEventListener("click", () => openProgramEditor(device, null));
    box.querySelectorAll("[data-prog-edit]").forEach((b) => b.addEventListener("click", () =>
      openProgramEditor(device, programs.find((p) => p.id === Number(b.dataset.progEdit)))));
    box.querySelectorAll("[data-prog-run]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try { const r = await api("POST", `/api/irrigation/programs/${b.dataset.progRun}/run`, { smart: true }); toast(r.reason); }
      catch (err) { toast(err.message); }
      b.disabled = false;
    }));
    box.querySelectorAll("[data-prog-toggle]").forEach((t) => t.addEventListener("change", async () => {
      const p = programs.find((x) => x.id === Number(t.dataset.progToggle));
      try { await api("PUT", `/api/irrigation/programs/${p.id}`, { ...p, enabled: t.checked }); }
      catch (err) { toast(err.message); t.checked = !t.checked; }
    }));
  } catch (err) { box.innerHTML = `<p class="note">${esc(err.message)}</p>`; }
}

function openProgramEditor(device, program) {
  const p = program || { name: "Lawn", starts: ["05:00"], days: ["mon", "wed", "fri"], smart: true, enabled: true,
    zones: device.zones.map((z) => ({ zone: Number(z.id), minutes: 10 })) };
  const minutesFor = (id) => p.zones.find((z) => String(z.zone) === String(id))?.minutes ?? 0;
  openSheet(program ? `Edit ${program.name}` : "New smart program", `
    <label class="field"><span>Name</span><input type="text" id="pg-name" value="${esc(p.name)}" maxlength="32"></label>
    <div class="field"><span>Start times</span><div class="inline">
      <input type="time" id="pg-start1" value="${esc(p.starts[0] || "05:00")}">
      <input type="time" id="pg-start2" value="${esc(p.starts[1] || "")}" aria-label="Second start time (optional)"></div></div>
    <div class="field"><span>Days</span>${dayChips("pg-day", p.days)}</div>
    <div class="field"><span>Minutes per zone at peak summer (0 skips the zone)</span><div class="card">${device.zones.map((z) => `
      <div class="row"><span class="main"><span class="name small">${esc(z.label)}</span></span>
        <input type="number" class="zone-area" data-pg-zone="${esc(z.id)}" min="0" max="120" value="${minutesFor(z.id)}" aria-label="Minutes for ${esc(z.label)}"></div>`).join("")}</div></div>
    <label class="field inline"><span>Adjust for weather (skip after rain, shorten when cool)</span>
      <span class="switch"><input type="checkbox" id="pg-smart" ${p.smart ? "checked" : ""}><span></span></span></label>
    <div class="inline"><button class="pill primary" id="pg-save">Save</button>
      ${program ? `<button class="pill danger" id="pg-del">Delete</button>` : ""}</div>
    <p class="note">Runs one zone after another. Like other hub schedules, it needs the Mac awake.</p>`);
  $("#pg-save", sheet).addEventListener("click", async (e) => {
    const body = {
      name: $("#pg-name", sheet).value.trim() || "Smart program",
      starts: [$("#pg-start1", sheet).value, $("#pg-start2", sheet).value].filter(Boolean),
      days: checkedDays("pg-day"),
      zones: [...sheet.querySelectorAll("[data-pg-zone]")].map((i) => ({ zone: Number(i.dataset.pgZone), minutes: Number(i.value) || 0 })),
      smart: $("#pg-smart", sheet).checked, enabled: program ? program.enabled : true,
    };
    if (!body.days.length) return toast("Pick at least one day");
    e.target.disabled = true;
    try {
      await api(program ? "PUT" : "POST", program ? `/api/irrigation/programs/${program.id}` : "/api/irrigation/programs", body);
      toast("Program saved"); sheet.close();
    } catch (err) { toast(err.message); e.target.disabled = false; }
  });
  $("#pg-del", sheet)?.addEventListener("click", async (e) => {
    if (e.target.dataset.armed !== "1") { e.target.dataset.armed = "1"; e.target.textContent = "Tap again to delete"; return; }
    try { await api("DELETE", `/api/irrigation/programs/${program.id}`); toast("Program deleted"); sheet.close(); }
    catch (err) { toast(err.message); }
  });
}

function openZoneSchedule(device, zone) {
  const sc = zone.schedule || { minutes: 10, starts: ["05:00"], days: [] };
  openSheet(`${zone.label}: schedule`, `
    <p class="note">This is saved on the controller itself, so it runs even when the Mac is off.</p>
    <label class="field"><span>Minutes</span><input type="number" id="zs-min" min="0" max="199" value="${sc.minutes}"></label>
    <label class="field"><span>Start times (up to 6, comma-separated, 10-minute steps)</span>
      <input type="text" id="zs-starts" value="${esc(sc.starts.join(", "))}" placeholder="05:00, 18:30"></label>
    <div class="field"><span>Days</span>${dayChips("zs-day", sc.days)}</div>
    <button class="pill primary" id="zs-save">Save to controller</button>`);
  $("#zs-save", sheet).addEventListener("click", async (e) => {
    const starts = $("#zs-starts", sheet).value.split(",").map((x) => x.trim()).filter(Boolean);
    e.target.disabled = true;
    try {
      await api("PUT", `/api/rainbird/zones/${zone.id}/schedule`, { minutes: Number($("#zs-min", sheet).value) || 0, starts, days: checkedDays("zs-day") });
      toast("Saved and confirmed on the controller"); sheet.close();
    } catch (err) { toast(err.message); e.target.disabled = false; }
  });
}


async function loadStorage() {
  const box = $("#storage-card");
  if (!box) return;
  try {
    const s = await api("GET", "/api/storage");
    const kb = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
    const plain = s.saved.filter((x) => x.kind === "password");
    box.innerHTML = `
      <div class="row"><div class="main"><span class="name small">Folder</span><span class="sub wrap">${esc(s.folder)} (only your Mac user can open it)</span></div></div>
      <div class="row"><div class="main"><span class="name small">Settings and logins</span>
        <span class="sub wrap">${esc(s.settings_file.name)}, ${kb(s.settings_file.bytes)}. Kept in memory and saved to disk on every change, with a backup copy.</span></div></div>
      <div class="row"><div class="main"><span class="name small">History and rooms</span>
        <span class="sub wrap">${esc(s.database.name)}, ${kb(s.database.bytes)}, crash-safe. ${s.data.filter((d) => d.rows).map((d) => `${esc(d.group)}: ${d.rows.toLocaleString()}`).join(" · ") || "Nothing yet."}</span></div></div>
      <div class="row"><div class="main"><span class="name small">Zone photos</span><span class="sub wrap">${s.photos.count} photo(s), ${kb(s.photos.bytes)}</span></div></div>
      <div class="row"><div class="main"><span class="name small">Saved settings (${s.saved.length})</span>
        <span class="sub wrap">${s.saved.map((x) => esc(x.label)).join(" · ") || "None yet."}</span></div></div>
      ${plain.length ? `<div class="row"><div class="main"><span class="name small">Plain-text passwords</span>
        <span class="sub wrap">${plain.map((x) => esc(x.label)).join("; ")}. These services only accept a password, so it is stored as is, readable by your Mac user. Use a separate account for the hub where you can, and keep the Mac's disk encrypted (FileVault) and backups private.</span></div></div>` : ""}`;
  } catch (err) { box.innerHTML = `<div class="row"><span class="sub">${esc(err.message)}</span></div>`; }
}


async function loadBackup() {
  const box = $("#backup-card");
  if (!box) return;
  let b;
  try { b = await api("GET", "/api/backup"); } catch (err) { box.innerHTML = `<div class="row"><span class="sub">${esc(err.message)}</span></div>`; return; }
  const when = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const mb = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  if (b.configured) {
    const ok = b.last_ok, bad = b.last && !b.last.ok ? b.last : null;
    box.innerHTML = `
      <div class="row"><div class="main"><span class="name">s3://${esc(b.bucket)}/${esc(b.prefix || "")}</span>
        <span class="sub wrap">Encrypted on this Mac before upload, every night at ${esc(b.runs_at)}. ${ok ? `Last backup ${esc(when(ok.at))}, ${mb(ok.bytes)}.` : "No backup yet."}</span></div></div>
      ${bad ? `<div class="row"><div class="main"><span class="name small">Last attempt failed</span><span class="sub wrap">${esc(when(bad.at))}: ${esc(bad.note)}</span></div></div>` : ""}
      <div class="row"><div class="inline" style="width:100%">
        <button class="pill primary" id="backup-run">Back up now</button>
        <button class="pill" id="backup-key">Recovery key</button>
        <button class="pill danger" id="backup-off">Turn off</button></div></div>`;
    $("#backup-run").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Backing up…";
      try { const r = await api("POST", "/api/backup/run"); toast(`Backed up ${mb(r.bytes)}`); } catch (err) { toast(err.message); }
      loadBackup();
    });
    $("#backup-key").addEventListener("click", showRecoveryKey);
    $("#backup-off").addEventListener("click", async (e) => {
      if (e.target.dataset.armed !== "1") { e.target.dataset.armed = "1"; e.target.textContent = "Tap again to turn off"; return; }
      try { await api("DELETE", "/api/backup"); toast("Backups turned off. Existing backups in S3 are untouched."); loadBackup(); } catch (err) { toast(err.message); }
    });
  } else {
    box.innerHTML = `
      <div class="row"><div class="main"><span class="name">Not set up</span>
        <span class="sub wrap">Nightly encrypted backup of your settings, logins, history and photos. The backup is encrypted on this Mac with a key that never leaves it, so S3 only holds ciphertext. The AWS key needs only <b>s3:PutObject</b> on the folder below.</span></div></div>
      <div class="row key-row"><input type="text" id="bk-bucket" placeholder="Bucket name" autocapitalize="none" spellcheck="false">
        <input type="text" id="bk-region" value="us-east-1" aria-label="AWS region" style="flex:0 0 9em">
        <input type="text" id="bk-prefix" value="smarthome" aria-label="Folder in the bucket" style="flex:0 0 9em"></div>
      <div class="row key-row"><input type="text" id="bk-akid" placeholder="AWS access key ID (blank: use the Mac's AWS login)" autocomplete="off" spellcheck="false">
        <input type="password" id="bk-secret" placeholder="AWS secret access key" autocomplete="off" spellcheck="false"></div>
      <div class="row"><label class="inline"><span>Include zone photos</span><span class="switch"><input type="checkbox" id="bk-photos" checked><span></span></span></label>
        <button class="pill primary" id="bk-connect" style="flex:none">Connect and back up now</button></div>`;
    $("#bk-connect").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Backing up…";
      try {
        const r = await api("PUT", "/api/backup", { bucket: $("#bk-bucket").value.trim(), region: $("#bk-region").value.trim(), prefix: $("#bk-prefix").value.trim(),
          access_key_id: $("#bk-akid").value.trim(), secret_access_key: $("#bk-secret").value.trim(), include_photos: $("#bk-photos").checked });
        toast(`First backup uploaded (${mb(r.bytes)})`);
        await loadBackup();
        showRecoveryKey();
      } catch (err) { toast(err.message); e.target.disabled = false; e.target.textContent = "Connect and back up now"; }
    });
  }
}

async function showRecoveryKey() {
  try {
    const { key } = await api("GET", "/api/backup/key");
    openSheet("Recovery key", `
      <p><b>Save this in your password manager now.</b> Without it a backup can't be opened, and if this Mac is lost the key goes with it.</p>
      <div class="card pad"><code id="rk" style="word-break:break-all;user-select:all">${esc(key)}</code></div>
      <div class="inline"><button class="pill" id="rk-copy">Copy</button><button class="pill primary" data-close>I've saved it</button></div>
      <p class="note">To restore on a new Mac: download the .enc file from S3, then run <code>python -m hub.backup restore FILE.enc --key KEY --to ~/.smarthome</code> (hub stopped).</p>`);
    $("#rk-copy", sheet).addEventListener("click", async () => { try { await navigator.clipboard.writeText(key); toast("Copied"); } catch { toast("Select the key and copy it"); } });
  } catch (err) { toast(err.message); }
}
