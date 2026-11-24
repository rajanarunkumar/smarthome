// ---------- chat: ask the home in plain language ----------
// Device changes happen straight away; renames, rooms and schedules come back as a plan to apply.
const chatState = { id: null, items: [], busy: false };
const CHAT_IDEAS = [
  "What's on right now?",
  "Turn off all the lights downstairs",
  "Review my device names",
  "Review my schedules",
  "Set the thermostat to 70",
];

function describeChange(st) {
  const words = {
    on: (v) => (v ? "on" : "off"), locked: () => "locked", brightness: (v) => `brightness ${v}%`, volume: (v) => `volume ${v}`,
    muted: (v) => (v ? "muted" : "unmuted"), heat_f: (v) => `heat to ${v}°`, cool_f: (v) => `cool to ${v}°`,
    thermostat_mode: (v) => ({ HEAT: "Heat", COOL: "Cool", HEATCOOL: "Heat · Cool", OFF: "Off" }[v] || v), eco: (v) => (v ? "Eco on" : "Eco off"),
    minutes: (v) => `for ${v} min`, zone: (v) => `zone ${v}`, rain_delay_days: (v) => (v ? `rain delay ${v} day(s)` : "rain delay off"),
    fan_minutes: (v) => (v ? `fan for ${v} min` : "fan auto"), level: (v) => `speed ${v}`, color: (v) => `colour ${v}`,
  };
  return Object.entries(st).map(([k, v]) => (words[k] ? words[k](v) : `${k.replace(/_/g, " ")} ${v}`)).join(", ");
}

function chatItemHtml(item, index) {
  if (item.role === "user") return `<div class="chat-msg me">${esc(item.text)}</div>`;
  if (item.role === "error") return `<div class="chat-msg err">${esc(item.text)}</div>`;
  const done = (item.done || []).map((d) => `<li>${esc(d.name)}: ${esc(describeChange(d.state))}</li>`).join("");
  const plan = item.plan;
  const planHtml = plan ? `
    <div class="card chat-plan" data-plan="${esc(plan.id)}" data-index="${index}">
      <div class="row"><div class="main"><span class="name small">${esc(plan.summary || "Proposed changes")}</span>
        <span class="sub">${plan.changes.length} change(s). Untick any you don't want.</span></div></div>
      ${plan.changes.map((c, i) => `
        <label class="row"><input type="checkbox" data-change="${i}" ${item.applied ? "disabled" : "checked"}>
          <span class="main"><span class="name small">${esc(c.label)}</span>${c.reason ? `<span class="sub wrap">${esc(c.reason)}</span>` : ""}</span>
          ${item.results ? `<span class="sub-value">${item.results[i] == null ? "skipped" : item.results[i].ok ? "✓" : "failed"}</span>` : ""}</label>`).join("")}
      ${(plan.problems || []).length ? `<div class="row"><span class="sub wrap">Left out: ${esc(plan.problems.join("; "))}</span></div>` : ""}
      ${item.applied ? "" : `<div class="row"><button class="pill primary" data-apply>Apply</button></div>`}
    </div>` : "";
  return `<div class="chat-msg bot">${esc(item.text).replace(/\n/g, "<br>")}${done ? `<ul class="chat-done">${done}</ul>` : ""}</div>${planHtml}`;
}

function renderChat() {
  const log = $("#chat-log", sheet);
  if (!log) return;
  log.innerHTML = chatState.items.length ? chatState.items.map(chatItemHtml).join("")
    : `<p class="note">Ask in your own words. Lights, speakers, the thermostat and sprinklers change straight away. Renaming, rooms and schedules come back as a plan you approve. Unlocking isn't possible from chat.</p>
       <div class="chat-ideas">${CHAT_IDEAS.map((t) => `<button class="pill" data-idea="${esc(t)}">${esc(t)}</button>`).join("")}</div>`;
  if (chatState.busy) log.insertAdjacentHTML("beforeend", `<div class="chat-msg bot typing">Thinking…</div>`);
  log.scrollTop = log.scrollHeight;
  log.querySelectorAll("[data-idea]").forEach((b) => b.addEventListener("click", () => chatSend(b.dataset.idea)));
  log.querySelectorAll("[data-apply]").forEach((b) => b.addEventListener("click", () => chatApply(b.closest("[data-plan]"))));
}

async function chatSend(text) {
  text = (text || "").trim();
  if (!text || chatState.busy) return;
  chatState.items.push({ role: "user", text });
  chatState.busy = true;
  renderChat();
  try {
    const r = await api("POST", "/api/chat", { message: text, conversation_id: chatState.id });
    chatState.id = r.conversation_id;
    chatState.items.push({ role: "bot", text: r.reply, done: r.done, plan: r.plan });
  } catch (err) {
    chatState.items.push({ role: "error", text: err.message });
  }
  chatState.busy = false;
  renderChat();
}

async function chatApply(card) {
  const item = chatState.items[Number(card.dataset.index)];
  const only = [...card.querySelectorAll("[data-change]")].filter((c) => c.checked).map((c) => Number(c.dataset.change));
  if (!only.length) { toast("Nothing ticked"); return; }
  const btn = card.querySelector("[data-apply]");
  btn.disabled = true; btn.textContent = "Applying…";
  try {
    const r = await api("POST", `/api/chat/plans/${encodeURIComponent(card.dataset.plan)}/apply`, { only });
    item.applied = true;
    item.results = item.plan.changes.map((_, i) => (only.includes(i) ? r.results[only.indexOf(i)] : null));
    const failed = r.results.filter((x) => !x.ok).length;
    toast(failed ? `${r.results.length - failed} applied, ${failed} failed` : `${r.results.length} change(s) applied`);
  } catch (err) { toast(err.message); btn.disabled = false; btn.textContent = "Apply"; }
  renderChat();
}

function openChat() {
  openSheet("Ask Home", `
    <div id="chat-log" class="chat-log"></div>
    <form id="chat-form" class="chat-form">
      <input type="text" id="chat-input" placeholder="e.g. Dim the living room to 30%" autocomplete="off" enterkeyhint="send" maxlength="2000">
      <button class="pill primary" type="submit">Send</button>
    </form>
    ${chatState.items.length ? `<button class="pill" id="chat-new" style="margin-top:8px">New conversation</button>` : ""}`);
  renderChat();
  $("#chat-form", sheet).addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#chat-input", sheet);
    chatSend(input.value);
    input.value = "";
  });
  $("#chat-new", sheet)?.addEventListener("click", () => { chatState.id = null; chatState.items = []; openChat(); });
}

$("#chat-open")?.addEventListener("click", openChat);
