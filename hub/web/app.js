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
const BRAND = { kasa: "Kasa", hue: "Hue", sonos: "Sonos", lg: "LG", govee: "Govee" };
// These brands have no rename API, so the hub keeps the name.
const HUB_NAMED = new Set(["govee", "lg"]);

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
  $("#title").textContent = { rooms: "Home", scenes: "Scenes", schedules: "Schedules", settings: "Settings" }[tab];
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
  else if (d.on && d.brightness) parts.push(`${d.brightness}%`);
  else parts.push(d.on ? "On" : "Off");
  return parts.join(" · ");
}

function deviceRow(d) {
  const dotStyle = d.on && d.color ? `style="background:${esc(d.color)}"` : "";
  return `
    <div class="row ${d.online ? "" : "offline"}">
      <span class="dot ${d.on ? "on" : ""}" ${dotStyle} aria-hidden="true"></span>
      <button class="main" data-open="${esc(d.id)}">
        <span class="name">${esc(d.name)}</span>
        <span class="sub">${esc(deviceSub(d))}</span>
      </button>
      <label class="switch" aria-label="${esc(d.name)} ${d.kind === "speaker" ? "play" : "power"}">
        <input type="checkbox" data-power="${esc(d.id)}" ${d.on ? "checked" : ""} ${d.online ? "" : "disabled"}>
        <span></span>
      </label>
    </div>`;
}

function renderRooms() {
  const groups = state.rooms.map((r) => ({ room: r, devices: state.devices.filter((d) => d.room_id === r.id) }));
  const unassigned = state.devices.filter((d) => d.room_id == null);
  let html = "";
  for (const { room, devices } of groups) {
    if (!devices.length) continue;
    html += `
      <div class="section-title"><span>${esc(room.name)}</span>
        <span class="actions">
          <button class="pill" data-room-power="${room.id}" data-on="true">All on</button>
          <button class="pill" data-room-power="${room.id}" data-on="false">All off</button>
        </span></div>
      <div class="card">${devices.map(deviceRow).join("")}</div>`;
  }
  if (unassigned.length) {
    html += `<div class="section-title"><span>No room yet</span></div>
      <div class="card">${unassigned.map(deviceRow).join("")}</div>
      <p class="note">Tap a device to rename it or put it in a room.</p>`;
  }
  if (!state.devices.length) html = `<p class="empty">Looking for devices… Kasa, Sonos and Govee appear on their own; pair the Hue Bridge and LG TV in Settings.</p>`;
  view.innerHTML = html + errorsHtml();
}

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
    ${caps.has("playback") && d.on !== false || d.kind === "speaker" ? `<div class="field"><span>${esc(d.media || (d.kind === "tv" ? "TV" : "Nothing playing"))}</span>
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
  $("#f-src", sheet)?.addEventListener("change", (e) => e.target.value && send({ source: e.target.value }));
  sheet.querySelectorAll("[data-play]").forEach((btn) => btn.addEventListener("click", async () => {
    const playback = btn.dataset.play;
    try {
      await api("POST", `/api/devices/${encodeURIComponent(id)}/state`, { playback });
      if (playback === "play" || playback === "pause") {
        d.playing = playback === "play";
        btn.dataset.play = d.playing ? "pause" : "play";
        btn.textContent = d.playing ? "⏸" : "▶";
      }
    } catch (err) { toast(err.message); }
  }));
}

// ---------- scenes ----------
function renderScenes() {
  if (!state.scenes.length) {
    view.innerHTML = `<p class="empty">No scenes yet. Hue scenes appear here after you pair the Hue Bridge in Settings.</p>`;
    return;
  }
  const groups = {};
  for (const s of state.scenes) (groups[s.group || "Other"] ||= []).push(s);
  view.innerHTML = Object.entries(groups).map(([group, scenes]) => `
    <div class="section-title"><span>${esc(group)}</span></div>
    <div class="scene-grid">${scenes.map((s) => `
      <div class="scene ${s.active ? "active" : ""}">
        <span class="name">${esc(s.name)}</span>
        <span class="acts">
          <button class="pill primary" data-scene="${esc(s.id)}">Turn on</button>
          ${s.dynamic ? `<button class="pill" data-scene="${esc(s.id)}" data-dynamic="1" title="Colors slowly shift">Dynamic</button>` : ""}
        </span>
      </div>`).join("")}</div>`).join("");
}

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
    <p class="note">Each schedule is saved on the device or bridge itself, so it runs even when this Mac is off.</p>
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
  const devices = state.devices.filter((d) => d.online && d.capabilities.includes("schedule"));
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
    <p class="note">Kasa plugs support times, sunrise and sunset. The Hue Bridge supports set times. Sonos, LG and Govee schedules aren't supported yet.</p>
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
      <span class="sub">${lg.available === false ? "Needs Python 3.12 on the hub Mac. See the README."
        : lg.paired ? (lg.wake_on_lan ? "The hub can turn the TV on and off." : "The hub can turn the TV off. To turn it on too, connect again while the TV is on.")
        : "Turn the TV on, tap Connect, then choose Accept on the TV with the remote within 30 seconds."}</span>
    </div>${lg.available === false ? "" : `<button class="pill ${lg.paired ? "" : "primary"}" id="lg-pair">${lg.paired ? "Connect again" : "Connect"}</button>`}</div></div>

    <div class="section-title"><span>Found automatically</span></div>
    <div class="card">
      <div class="row"><div class="main"><span class="name">Kasa</span><span class="sub">${count("kasa")} plug(s) and dimmer(s)</span></div></div>
      <div class="row"><div class="main"><span class="name">Sonos</span><span class="sub">${count("sonos")} speaker(s)</span></div></div>
      <div class="row"><div class="main"><span class="name">Govee</span><span class="sub">${count("govee")} light(s). A light only shows up after you turn on <b>LAN Control</b> for it in the Govee app.</span></div></div>
    </div>

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

async function afterSignIn(offerPasskey) {
  state.me = await api("GET", "/api/auth/me");
  state.signedIn = state.me.signed_in;
  document.querySelector(".tabs").hidden = false;
  switchTab(state.tab);
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
