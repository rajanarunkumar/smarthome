// ---------- Home Assistant: the connection, and moving each brand to it ----------
function haSettingsCard() {
  const h = state.status?.ha || {};
  return `
    <div class="section-title"><span>Home Assistant</span></div>
    <div class="card">${h.connected ? `
      <div class="row"><div class="main"><span class="name">${h.online ? "Connected" : "Connecting…"}${h.version ? ` · ${esc(h.version)}` : ""}</span>
        <span class="sub wrap">${esc(h.url || "")} · ${h.entities ?? 0} entities${h.devices ? ` · ${h.devices} device(s) served from HA` : ""}${h.error ? ` · ${esc(h.error)}` : ""}</span></div>
        <button class="pill primary" id="ha-move">Move to Home Assistant…</button><button class="pill danger" id="ha-off">Disconnect</button></div>` : `
      <div class="row"><div class="main"><span class="name">Not connected</span>
        <span class="sub wrap">Home Assistant takes over the device connections; the hub keeps the insights (see docs/PLAN-HOME-ASSISTANT.md).
          In Home Assistant: your profile → Security → <b>Long-lived access tokens</b> → Create token, and paste it here.</span></div></div>
      <div class="row key-row"><input type="url" id="ha-url" placeholder="http://homeassistant.local:8123" autocomplete="off" spellcheck="false">
        <input type="password" id="ha-token" placeholder="Long-lived access token" autocomplete="off" spellcheck="false">
        <button class="pill primary" id="ha-on">Connect</button></div>`}
    </div>`;
}

function bindHaSettings(accountButton) {
  accountButton("#ha-on", () => api("PUT", "/api/ha/account", { url: ($("#ha-url").value.trim() || "http://homeassistant.local:8123"), token: $("#ha-token").value.trim() }),
    (r) => `Home Assistant ${r.version || ""} connected: ${r.entities} entities`);
  accountButton("#ha-off", () => api("DELETE", "/api/ha/account"), () => "Home Assistant disconnected; every brand is back on the hub");
  $("#ha-move")?.addEventListener("click", openMigration);
}

const MIGRATION_STATE = (r) => r.stays ? ["stays", "Stays in the hub"]
  : r.moved ? ["done", "✓ Moved"]
  : r.source === "ha" ? ["trial", "On Home Assistant (trial)"]
  : !r.hub_connected ? ["idle", "Not used"]
  : r.ha_entities ? ["ready", "Found in Home Assistant"] : ["todo", "Not in Home Assistant yet"];

async function openMigration() {
  openSheet("Move to Home Assistant", `<p class="note">Checking Home Assistant…</p>`);
  let rep;
  try { rep = await api("GET", "/api/migration"); } catch (err) { $(".sheet-body", sheet).innerHTML = `<p class="note">${esc(err.message)}</p>`; return; }
  const moved = rep.rows.filter((r) => r.moved || r.stays || !r.hub_connected).length;
  $(".sheet-body", sheet).innerHTML = `
    <p class="note">${rep.done ? "Every connection is either moved or stays in the hub. Done." : `${moved} of ${rep.rows.length} settled.`}
      For each brand: add it in Home Assistant (Settings → Devices & services), check its devices there, switch the source to Home Assistant,
      use it for a week, then remove it from the hub. Passwords and tokens are never shown here.</p>
    ${rep.rows.map((r) => {
      const [cls, label] = MIGRATION_STATE(r);
      return `<div class="card mig ${cls}">
        <div class="row"><div class="main"><span class="name">${esc(r.label)}</span>
          <span class="sub wrap">${esc(label)}${r.stays ? ` · ${esc(r.note || "")}` : r.hub_devices != null ? ` · hub ${r.hub_devices} device(s), Home Assistant found ${r.matched} of them${r.ha_entities ? ` (${r.ha_entities} entities)` : ""}` : ""}</span></div></div>
        ${r.stays ? "" : `
        ${r.unmatched?.length && r.ha_entities ? `<div class="row"><span class="sub wrap">Not matched by name yet: ${esc(r.unmatched.join(", "))}. Rename them in Home Assistant to match, or ignore if they're gone.</span></div>` : ""}
        ${r.moved ? "" : `<details class="mig-help"><summary class="sub">What you'll need and the steps</summary><div class="sub wrap">${esc(r.needs)}${r.hints?.length ? `<br>${r.hints.map(esc).join("<br>")}` : ""}<br><b>Steps:</b> ${esc(r.steps)}</div></details>`}
        ${r.moved ? "" : `<div class="row">
          <label class="inline"><span class="sub">Source</span><select data-mig-source="${esc(r.brand)}">
            <option value="hub" ${r.source === "hub" ? "selected" : ""}>Hub</option>
            <option value="ha" ${r.source === "ha" ? "selected" : ""} ${r.ha_entities ? "" : "disabled"}>Home Assistant</option></select></label>
          ${r.source === "ha" ? `<button class="pill danger" data-mig-remove="${esc(r.brand)}">Remove from hub</button>` : ""}</div>`}`}
      </div>`;
    }).join("")}`;
  sheet.querySelectorAll("[data-mig-source]").forEach((sel) => sel.addEventListener("change", async () => {
    try { await api("PUT", `/api/migration/${sel.dataset.migSource}`, { source: sel.value }); toast(sel.value === "ha" ? "Now from Home Assistant" : "Back on the hub"); openMigration(); }
    catch (err) { toast(err.message); openMigration(); }
  }));
  sheet.querySelectorAll("[data-mig-remove]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = "Tap again: delete the hub's login"; return; }
    try { await api("POST", `/api/migration/${b.dataset.migRemove}/remove`); toast("Removed from the hub"); openMigration(); }
    catch (err) { toast(err.message); }
  }));
}
