"""Chat with the home: plain-language control, questions, and tidying up names, rooms and schedules.

The model, either Google Gemini (free tier, key in Settings → Ask Home) or Claude (the Anthropic key in
Settings → Photo analysis), gets a few tools that call the hub itself. Two kinds of action:
- Device control (lights, speakers, thermostats, sprinklers…) runs straight away, through the same path
  as tapping in the app. Unlocking a lock is never possible from chat.
- Changes to the setup (renaming, moving to a room, creating, deleting or pausing schedules) come back
  as a plan. Nothing changes until the person taps Apply.

The conversation stays in the hub's memory (not on disk) for an hour, and is sent to the chosen provider
for each reply. The loop runs here, on the Mac, because the tools act on the home network.
"""

from __future__ import annotations

import json
import logging
import secrets as pysecrets
import time
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from .models import DAYS, HubError, ScheduleIn, StateIn

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
GEMINI_MODEL = "gemini-flash-lite-latest"   # Google's alias for the newest Flash-Lite: the largest free daily quota

# Models the person can pick, per kind of request. "provider:model".
MODEL_CHOICES = {
    "gemini:gemini-flash-lite-latest": "Gemini Flash-Lite · free, most free use",
    "gemini:gemini-flash-latest": "Gemini Flash · free, fewer requests a day",
    "claude:claude-haiku-5-5": "Claude Haiku 5.5 · about 0.2¢ a command",
    "claude:claude-sonnet-5-5": "Claude Sonnet 5.5 · about 3¢ a command",
    "claude:claude-opus-5-5": "Claude Opus 5.5 · about 5–10¢ a command",
}
MODES = ("quick", "thorough")
EFFORT = {"quick": "low", "thorough": "medium"}
# Words that mean "think it through" rather than "do this": reviews and tidying go to the thorough model.
THOROUGH_WORDS = ("review", "tidy", "clean up", "cleanup", "organize", "organise", "optimize", "optimise", "improve",
                  "suggest", "recommend", "audit", "reorgan", "rename all", "naming", "best way", "why ", "explain",
                  "analy", "compare", "plan ")
HISTORY_RECAP = 6         # exchanges carried over when a conversation moves between Gemini and Claude


OPENAI_SKIP = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "instruct", "moderation",
               "dall", "codex", "computer", "deep-research", "chat-latest")


def openai_chat_models(models: list) -> list[str]:
    """OpenAI model ids that can chat and call tools, newest first, without dated snapshots or special-purpose ones."""
    import re
    keep = [m for m in models if (m.id.startswith("gpt-") or re.match(r"^o\d", m.id))
            and not any(w in m.id for w in OPENAI_SKIP) and not re.search(r"-\d{4}-\d{2}-\d{2}$", m.id)]
    return [m.id for m in sorted(keep, key=lambda m: getattr(m, "created", 0) or 0, reverse=True)][:12]


def pick_mode(text: str) -> str:
    t = text.lower()
    return "thorough" if any(w in t for w in THOROUGH_WORDS) else "quick"
MAX_ROUNDS = 8            # tool rounds per message
KEEP_SECONDS = 3600
MAX_CONVERSATIONS = 20
MAX_MESSAGES = 80         # a longer conversation starts afresh (history is never edited, only appended to)

SYSTEM = """You are the assistant inside a private smart-home hub app. You can see and control the devices in this \
home through the tools, and help the owner keep names, rooms and schedules tidy.

How to act:
- To control devices (on/off, brightness, colour, volume, thermostat mode and temperatures, running a sprinkler \
zone, Ring motion detection, appliance buttons), call control_device directly. Look up device ids with \
list_devices first; never guess an id. After acting, say briefly what you did.
- Never unlock a lock or open a garage door. If asked, say the person needs to do it themselves in the app \
(it asks for a second tap there). Locking is fine.
- Changes to the setup — renaming devices, moving devices between rooms, creating rooms, creating, deleting, \
pausing or resuming schedules — must go through propose_changes. They are only applied when the person taps \
Apply, so say that the plan is ready to review; don't claim it's done.
- If a request is ambiguous (two devices match, or you aren't sure which room), ask a short question instead of \
guessing. If something can't be done with these tools, say so.
- Answer in a few short sentences or a short list. The app is used on a phone.

Naming advice, when asked to review or improve names:
- Use "Room + what it is": "Kitchen Island Lights", "Primary Bedroom Fan", "Back Yard Sprinklers".
- Every name unique, 2–4 words, no brand or model names (Govee, Kasa, H6008), no serial numbers, no "Plug 1".
- Voice-friendly for Alexa and Google Home: easy to say, not too close to another name ("Lamp" vs "Lamps"), no \
digits where a word works, avoid names that sound like commands ("Off Lamp").
- Name a smart plug after what's plugged in ("Christmas Tree"), not "Plug".
- Put every device in the right room; a device's room should not be repeated in its name only when the person \
asks for short names.
- Only propose changes that improve something; explain the pattern in one line, not each rename.

Schedule advice, when asked to review schedules:
- Prefer sunset/sunrise triggers (with an offset) for outdoor and evening lights over fixed times.
- Look for duplicates, conflicts (an on and an off at the same time), schedules for devices that are offline \
or no longer exist, and lights left on all night.
- Big electrical loads (pool pump, laundry, EV) are cheaper in cheaper hours if the person mentions a time-of-use \
or free-nights electricity plan; ask before assuming one.
- Schedules for brands without native schedules run in the hub, so the Mac must stay awake — mention it once \
if relevant. Locks can't be scheduled.
"""

STATE_PROPS = {
    "on": {"type": "boolean"}, "brightness": {"type": "integer"}, "color": {"type": "string"},
    "volume": {"type": "integer"}, "muted": {"type": "boolean"},
    "playback": {"type": "string", "enum": ["play", "pause", "next", "previous"]}, "source": {"type": "string"},
    "action": {"type": "string"}, "level": {"type": "integer"}, "locked": {"type": "boolean"},
    "zone": {"type": "integer"}, "minutes": {"type": "integer"}, "rain_delay_days": {"type": "integer"},
    "thermostat_mode": {"type": "string", "enum": ["HEAT", "COOL", "HEATCOOL", "OFF"]},
    "heat_f": {"type": "number"}, "cool_f": {"type": "number"}, "eco": {"type": "boolean"}, "fan_minutes": {"type": "integer"},
}

TOOLS: list[dict] = [
    {
        "name": "list_devices",
        "description": "Every device in the home: id, name, room, brand, kind, whether it's online, its current state "
                       "and what it can do. Call this before controlling or renaming anything.",
        "input_schema": {"type": "object", "properties": {
            "room": {"type": "string", "description": "Only devices in this room (optional)."}}},
    },
    {
        "name": "list_schedules",
        "description": "Every schedule: id, name, devices, on/off and brightness, trigger (time or sunrise/sunset with "
                       "offset), days, enabled, and where it runs (on the device, in a brand's cloud, or in the hub).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "control_device",
        "description": "Change one device now. state fields (send only what should change): on (bool), brightness (1-100), "
                       "color (#rrggbb), volume (0-100), muted (bool), playback (play|pause|next|previous), source, "
                       "action (an id from the device's actions), level (1-10, fan speed), locked (true only: lock), "
                       "zone (1-64) with minutes (1-240) for sprinklers, rain_delay_days (0-14), thermostat_mode "
                       "(HEAT|COOL|HEATCOOL|OFF), heat_f, cool_f (°F), eco (bool), fan_minutes (0-720).",
        "input_schema": {"type": "object", "properties": {
            "device_id": {"type": "string"},
            "state": {"type": "object", "properties": STATE_PROPS}}, "required": ["device_id", "state"]},
    },
    {
        "name": "propose_changes",
        "description": "Propose setup changes for the person to approve with one tap. Each change has a type and a short "
                       "reason. Types and their fields: rename {device_id, name (max 32 chars)}; move {device_id, room "
                       "(room name; created if new)}; create_schedule {name, device_ids, on (bool), brightness (1-100, "
                       "optional), trigger (time|sunrise|sunset), time (HH:MM, for time), offset_min (-180..180, for "
                       "sunrise/sunset), days (mon..sun)}; delete_schedule {schedule_id}; pause_schedule {schedule_id}; "
                       "resume_schedule {schedule_id}.",
        "input_schema": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "One sentence: what the plan does overall."},
            "changes": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["rename", "move", "create_schedule", "delete_schedule",
                                                   "pause_schedule", "resume_schedule"]},
                "reason": {"type": "string"},
                "device_id": {"type": "string"}, "name": {"type": "string"}, "room": {"type": "string"},
                "schedule_id": {"type": "string"},
                "device_ids": {"type": "array", "items": {"type": "string"}},
                "on": {"type": "boolean"}, "brightness": {"type": "integer"},
                "trigger": {"type": "string", "enum": ["time", "sunrise", "sunset"]},
                "time": {"type": "string", "description": "HH:MM, 24-hour"}, "offset_min": {"type": "integer"},
                "days": {"type": "array", "items": {"type": "string", "enum": DAYS}}},
                "required": ["type"]}}},
            "required": ["summary", "changes"]},
    },
]


def device_row(d: Any, rooms: dict[int, str]) -> dict:
    """What Claude needs to know about a device, without the noise."""
    row: dict[str, Any] = {"id": d.id, "name": d.name, "room": rooms.get(d.room_id), "brand": d.brand, "kind": d.kind,
                           "online": d.online}
    for field in ("on", "brightness", "color", "volume", "muted", "playing", "media", "battery", "level", "rain_delay"):
        value = getattr(d, field, None)
        if value not in (None, "", []):
            row[field] = value
    if d.kind == "lock":
        row["locked"] = bool(d.on)
    if d.thermostat:
        row["thermostat"] = {k: v for k, v in d.thermostat.items() if k in ("current", "humidity", "mode", "modes", "hvac", "heat", "cool", "eco")}
    if d.zones:
        row["zones"] = [{"zone": z.get("id"), "name": z.get("name") or z.get("label"), "running": z.get("running")} for z in d.zones]
    if d.actions:
        row["actions"] = [{"id": a["id"], "label": a["label"]} for a in d.actions]
    if d.sources:
        row["sources"] = [s["label"] for s in d.sources][:15]
    row["can"] = d.capabilities
    return row


def schedule_row(s: Any) -> dict:
    trigger = None
    if s.trigger:
        trigger = s.trigger.time if s.trigger.type == "time" else f"{s.trigger.type}{s.trigger.offset_min:+d}min"
    return {"id": s.id, "name": s.name, "devices": s.device_names or s.device_ids, "device_ids": s.device_ids,
            "action": s.action.model_dump(exclude_none=True) if s.action else None, "trigger": trigger, "days": s.days,
            "enabled": s.enabled, "runs_on": s.runs_on, "summary": s.summary,
            "can_delete": s.can_delete, "can_pause": s.can_toggle}


class Chat:
    def __init__(self, hub: Any, client_factory=None, gemini_factory=None, openai_factory=None) -> None:
        self.hub = hub
        self._client_factory = client_factory
        self._gemini_factory = gemini_factory
        self._openai_factory = openai_factory
        self._conversations: dict[str, dict] = {}
        self._plans: dict[str, dict] = {}

    def keys(self) -> dict[str, bool]:
        return {"gemini": bool((self.hub.secrets.get("gemini") or {}).get("api_key")),
                "claude": bool(self.hub.secrets.get("anthropic_api_key")),
                "openai": bool((self.hub.secrets.get("openai") or {}).get("api_key"))}

    def _openai_models(self) -> list[str]:
        return list((self.hub.secrets.get("openai") or {}).get("models") or [])

    def catalog(self) -> dict[str, str]:
        """Every model that can be chosen now: the fixed Gemini and Claude list plus the OpenAI models the key can use."""
        out = dict(MODEL_CHOICES)
        for m in self._openai_models():
            out[f"openai:{m}"] = f"OpenAI {m} · paid"
        return out

    async def connect_openai(self, key: str) -> list[str]:
        """Check the key by asking which models it can use, and remember the ones that can chat."""
        import openai
        client = self._openai_factory(key) if self._openai_factory else openai.AsyncOpenAI(api_key=key)
        try:
            models = [m async for m in client.models.list()]
        except openai.AuthenticationError as exc:
            raise HubError(400, "OpenAI rejected the API key.") from exc
        except openai.APIConnectionError as exc:
            raise HubError(504, "Couldn't reach OpenAI. Check the Mac's internet connection.") from exc
        except openai.APIStatusError as exc:
            raise HubError(502, f"OpenAI said {exc.status_code} when listing models.") from exc
        ids = openai_chat_models(models)
        if not ids:
            raise HubError(400, "This OpenAI key can't use any chat models.")
        self.hub.secrets.set("openai", {"api_key": key, "models": ids})
        return ids

    def model_for(self, mode: str) -> str | None:
        """The "provider:model" for quick commands or thorough reviews: the person's choice if its key is saved,
        otherwise the best default for what's available (free Gemini for quick, Claude Opus for thorough)."""
        keys = self.keys()
        cfg = self.hub.secrets.get("chat") or {}
        chosen = (cfg.get("models") or {}).get(mode)
        if not chosen and cfg.get("provider"):               # "use this provider": its quick or thorough model
            chosen = {("gemini", "quick"): "gemini:" + self._gemini_model(), ("gemini", "thorough"): "gemini:gemini-flash-latest",
                      ("claude", "quick"): "claude:claude-haiku-5-5", ("claude", "thorough"): f"claude:{MODEL}"}.get((cfg["provider"], mode))
        if chosen and keys.get(chosen.split(":", 1)[0]) and not (chosen.startswith("openai:") and chosen[7:] not in self._openai_models()):
            return chosen
        if chosen and chosen.startswith("openai:") and chosen[7:] not in self._openai_models():
            chosen = None
        oa = self._openai_models()
        oa_quick = next((m for m in oa if "mini" in m), oa[0] if oa else None)
        oa_thorough = next((m for m in oa if "mini" not in m and "nano" not in m), oa[0] if oa else None)
        order = (["gemini:" + self._gemini_model(), "claude:claude-haiku-5-5", f"openai:{oa_quick}"] if mode == "quick"
                 else [f"claude:{MODEL}", f"openai:{oa_thorough}", "gemini:gemini-flash-latest"])
        return next((m for m in order if keys[m.split(":", 1)[0]] and not m.endswith(":None")), None)

    def _gemini_model(self) -> str:
        return (self.hub.secrets.get("gemini") or {}).get("model") or GEMINI_MODEL

    def provider(self) -> str | None:
        """Which provider answers quick commands (shown in Settings)."""
        model = self.model_for("quick")
        return model.split(":", 1)[0] if model else None

    def choices(self) -> dict:
        keys = self.keys()
        return {"models": {mode: self.model_for(mode) for mode in MODES},
                "available": [{"id": k, "label": v} for k, v in self.catalog().items() if keys[k.split(":", 1)[0]]]}

    def _client(self) -> Any:
        key = self.hub.secrets.get("anthropic_api_key")
        if not key:
            raise HubError(409, "Add a Gemini key in Settings → Ask Home (free), or an Anthropic key in Settings → Photo analysis.")
        if self._client_factory:
            return self._client_factory(key)
        import anthropic
        return anthropic.AsyncAnthropic(api_key=key)

    def _prune(self) -> None:
        now = time.time()
        for store in (self._conversations, self._plans):
            for k in [k for k, v in store.items() if now - v["at"] > KEEP_SECONDS]:
                del store[k]
        while len(self._conversations) > MAX_CONVERSATIONS:
            del self._conversations[min(self._conversations, key=lambda k: self._conversations[k]["at"])]

    # ---- tools ----
    async def _rooms(self) -> dict[int, str]:
        return {r["id"]: r["name"] for r in self.hub.store.rooms()}

    async def _run_tool(self, name: str, args: dict, turn: dict) -> str:
        if name == "list_devices":
            devices, errors = await self.hub.devices()
            rooms = await self._rooms()
            rows = [device_row(d, rooms) for d in devices]
            if args.get("room"):
                want = str(args["room"]).strip().lower()
                rows = [r for r in rows if (r["room"] or "").lower() == want]
            return json.dumps({"devices": rows, "rooms": sorted(rooms.values()), "errors": errors}, default=str)
        if name == "list_schedules":
            schedules, errors = await self.hub.named_schedules()
            return json.dumps({"schedules": [schedule_row(s) for s in schedules], "errors": errors}, default=str)
        if name == "control_device":
            device_id = str(args.get("device_id", ""))
            try:
                state = StateIn(**(args.get("state") or {}))
            except ValidationError as exc:
                raise HubError(400, f"Invalid state: {exc.errors()[0]['msg']}") from exc
            if state.locked is False:
                raise HubError(403, "Unlocking isn't possible from chat. The person can unlock in the app.")
            if not state.model_fields_set:
                raise HubError(400, "Nothing to change.")
            known = self.hub.last_devices or {}
            if known and device_id not in known:
                raise HubError(404, f"No device with id {device_id}. Call list_devices.")
            await self.hub.set_state(device_id, state)
            label = known.get(device_id).name if device_id in known else device_id
            turn["done"].append({"device_id": device_id, "name": label,
                                 "state": state.model_dump(exclude_none=True, exclude_unset=True)})
            return "Done."
        if name == "propose_changes":
            plan = await self._make_plan(args)
            turn["plan"] = plan
            return f"Plan {plan['id']} with {len(plan['changes'])} change(s) is shown to the person with an Apply button."
        raise HubError(400, f"Unknown tool {name}")

    async def _make_plan(self, args: dict) -> dict:
        """Check every proposed change now, so the person only sees changes that can be applied."""
        devices, _ = await self.hub.devices()
        by_id = {d.id: d for d in devices}
        schedules, _ = await self.hub.named_schedules()
        sched_by_id = {s.id: s for s in schedules}
        changes, problems = [], []
        for raw in args.get("changes") or []:
            kind = raw.get("type")
            reason = str(raw.get("reason") or "")[:200]
            try:
                if kind == "rename":
                    d = by_id.get(raw.get("device_id"))
                    name = str(raw.get("name") or "").strip()
                    if d is None or not (1 <= len(name) <= 32):
                        raise ValueError("needs a known device_id and a name of 1-32 characters")
                    if name == d.name:
                        continue
                    changes.append({"type": kind, "device_id": d.id, "name": name, "reason": reason,
                                    "label": f"Rename “{d.name}” to “{name}”"})
                elif kind == "move":
                    d = by_id.get(raw.get("device_id"))
                    room = str(raw.get("room") or "").strip()
                    if d is None or not (1 <= len(room) <= 40):
                        raise ValueError("needs a known device_id and a room name")
                    changes.append({"type": kind, "device_id": d.id, "room": room, "reason": reason,
                                    "label": f"Move “{d.name}” to {room}"})
                elif kind == "create_schedule":
                    trigger = {"type": raw.get("trigger") or "time"}
                    if trigger["type"] == "time":
                        trigger["time"] = raw.get("time")
                    else:
                        trigger["offset_min"] = int(raw.get("offset_min") or 0)
                    action = {"on": bool(raw.get("on"))}
                    if raw.get("brightness"):
                        action["brightness"] = int(raw["brightness"])
                    spec = ScheduleIn(name=str(raw.get("name") or "Schedule")[:32], device_ids=list(raw.get("device_ids") or []),
                                      action=action, trigger=trigger, days=list(raw.get("days") or DAYS))
                    missing = [i for i in spec.device_ids if i not in by_id]
                    if missing:
                        raise ValueError(f"unknown device ids {missing}")
                    when = spec.trigger.time if spec.trigger.type == "time" else f"{spec.trigger.type} {spec.trigger.offset_min:+d} min"
                    names = ", ".join(by_id[i].name for i in spec.device_ids)
                    days = "every day" if len(spec.days) == 7 else ", ".join(d.title() for d in spec.days)
                    changes.append({"type": kind, "spec": spec.model_dump(), "reason": reason,
                                    "label": f"New schedule “{spec.name}”: {names} {'on' if spec.action.on else 'off'} at {when}, {days}"})
                elif kind in ("delete_schedule", "pause_schedule", "resume_schedule"):
                    s = sched_by_id.get(raw.get("schedule_id"))
                    if s is None:
                        raise ValueError("unknown schedule_id")
                    if kind == "delete_schedule" and not s.can_delete or kind != "delete_schedule" and not s.can_toggle:
                        raise ValueError(f"schedule {s.name} can't be changed from the hub")
                    verb = {"delete_schedule": "Delete", "pause_schedule": "Pause", "resume_schedule": "Resume"}[kind]
                    changes.append({"type": kind, "schedule_id": s.id, "reason": reason, "label": f"{verb} schedule “{s.name}”"})
                else:
                    raise ValueError("unknown change type")
            except (ValueError, ValidationError, TypeError) as exc:
                msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                problems.append(f"{kind}: {msg}")
        if problems and not changes:
            raise HubError(400, "None of the changes can be applied: " + "; ".join(problems))
        plan = {"id": "plan_" + pysecrets.token_hex(6), "summary": str(args.get("summary") or "")[:300], "changes": changes,
                "problems": problems, "at": time.time(), "applied": False}
        self._plans[plan["id"]] = plan
        return plan

    async def apply_plan(self, plan_id: str, only: list[int] | None = None) -> dict:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise HubError(404, "That plan expired. Ask again in the chat.")
        if plan["applied"]:
            raise HubError(409, "That plan was already applied.")
        plan["applied"] = True
        results = []
        for i, c in enumerate(plan["changes"]):
            if only is not None and i not in only:
                continue
            try:
                if c["type"] == "rename":
                    await self.hub.rename_device(c["device_id"], c["name"])
                elif c["type"] == "move":
                    room = self.hub.store.room_by_name(c["room"]) or self.hub.store.add_room(c["room"])
                    self.hub.store.set_device_room(c["device_id"], room["id"])
                elif c["type"] == "create_schedule":
                    await self.hub.create_schedules(ScheduleIn(**c["spec"]))
                elif c["type"] == "delete_schedule":
                    await self.hub.adapter_for(c["schedule_id"]).delete_schedule(c["schedule_id"])
                else:
                    await self.hub.adapter_for(c["schedule_id"]).set_schedule_enabled(c["schedule_id"], c["type"] == "resume_schedule")
                results.append({"label": c["label"], "ok": True})
            except Exception as exc:  # noqa: BLE001 - report each change on its own
                results.append({"label": c["label"], "ok": False, "error": str(getattr(exc, "message", None) or exc)})
        self.hub.schedule_cache.invalidate_all()
        for conv in self._conversations.values():   # tell Claude next turn what happened
            if conv.get("plan_ids") and plan_id in conv["plan_ids"]:
                conv.setdefault("notes", []).append(
                    f"The person applied plan {plan_id}: " + "; ".join(f"{r['label']} ({'done' if r['ok'] else 'failed: ' + r.get('error', '')})" for r in results))
        return {"results": results}

    # ---- conversation ----
    async def send(self, text: str, conversation_id: str | None = None, mode: str = "auto",
                   model: str | None = None) -> dict:
        """mode: quick | thorough | auto (guessed from the words). model: a "provider:model" for this message only."""
        self._prune()
        mode = mode if mode in MODES else pick_mode(text)
        if model:
            if model not in self.catalog() and not model.startswith("gemini:gemini-"):
                raise HubError(400, f"Unknown model {model}")
            if not self.keys().get(model.split(":", 1)[0]):
                raise HubError(409, f"There's no {model.split(':', 1)[0].title()} key saved in Settings.")
        spec = model or self.model_for(mode)
        if spec is None:
            raise HubError(409, "Add a Gemini key in Settings → Ask Home (free), or an Anthropic key in Settings → Photo analysis.")
        provider, model_id = spec.split(":", 1)
        conv = self._conversations.get(conversation_id or "")
        if conv is not None and len(conv["messages"]) > MAX_MESSAGES:
            del self._conversations[conversation_id]
            conv = None
        if conv is None:
            conversation_id = "c_" + pysecrets.token_hex(8)
            conv = {"messages": [], "at": time.time(), "plan_ids": [], "provider": provider, "transcript": []}
            self._conversations[conversation_id] = conv
        recap = ""
        if conv["provider"] != provider:
            # Gemini and Claude can't read each other's turns: start the model's history afresh, with a plain recap.
            conv["messages"], conv["provider"] = [], provider
            recap = "".join(f"\nEarlier — person: {u}\nEarlier — you: {r}" for u, r in conv["transcript"][-HISTORY_RECAP:])
            recap = f"\n[Recap of this conversation so far, answered by another model:{recap}]" if recap else ""
        conv["at"] = time.time()
        now = datetime.now().astimezone().strftime("%A %-d %B %Y, %-I:%M %p %Z")
        notes = "".join(f"\n[{n}]" for n in conv.pop("notes", []))
        prompt = f"[Now: {now}]{recap}{notes}\n{text.strip()}"
        turn: dict = {"done": [], "plan": None}
        if provider == "gemini":
            reply = await self._gemini_loop(conv["messages"], prompt, turn, model_id)
        elif provider == "openai":
            reply = await self._openai_loop(conv["messages"], prompt, turn, model_id, EFFORT[mode])
        else:
            reply = await self._claude_loop(conv["messages"], prompt, turn, model_id, EFFORT[mode])
        reply = reply or "Done."
        conv["transcript"].append((text.strip()[:500], reply[:800]))
        if turn["plan"]:
            conv["plan_ids"].append(turn["plan"]["id"])
        plan = turn["plan"]
        return {"conversation_id": conversation_id, "reply": reply, "done": turn["done"], "provider": provider,
                "model": spec, "model_label": self.catalog().get(spec, model_id).split(" · ")[0], "mode": mode,
                "plan": {k: plan[k] for k in ("id", "summary", "changes", "problems")} if plan else None}

    async def _tool_result(self, name: str, args: dict, turn: dict) -> tuple[str, bool]:
        try:
            return await self._run_tool(name, args, turn), False
        except HubError as exc:
            return exc.message, True
        except Exception as exc:  # noqa: BLE001
            log.warning("chat tool %s failed: %s", name, exc)
            return f"{type(exc).__name__}: {exc}", True

    async def _claude_loop(self, messages: list, prompt: str, turn: dict, model: str = MODEL, effort: str = "medium") -> str:
        client = self._client()
        start = len(messages)
        messages.append({"role": "user", "content": prompt})
        import anthropic
        try:
            for _ in range(MAX_ROUNDS):
                extra = {} if "haiku" in model else {   # Haiku has no server-side fallback
                    "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                response = await client.beta.messages.create(
                    model=model, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
                    output_config={"effort": effort}, cache_control={"type": "ephemeral"}, **extra)
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason == "refusal":
                    reply = "Claude declined to help with that."
                    break
                if response.stop_reason == "pause_turn":
                    continue
                uses = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason != "tool_use" or not uses:
                    reply = "\n".join(b.text for b in response.content if b.type == "text").strip()
                    if response.stop_reason == "max_tokens":
                        reply += "\n\n(The answer was cut short.)"
                    break
                results = []
                for use in uses:     # one at a time: device changes may depend on each other
                    out, is_error = await self._tool_result(use.name, dict(use.input or {}), turn)
                    results.append({"type": "tool_result", "tool_use_id": use.id, "content": out, "is_error": is_error})
                messages.append({"role": "user", "content": results})
            else:
                reply = "That took too many steps. Try asking for one thing at a time."
        except anthropic.AuthenticationError as exc:
            del messages[start:]
            raise HubError(400, "Anthropic rejected the API key. Check it in Settings → Photo analysis.") from exc
        except anthropic.RateLimitError as exc:
            del messages[start:]
            raise HubError(429, "Claude is busy right now. Try again in a minute.") from exc
        except anthropic.APIStatusError as exc:
            del messages[start:]
            raise HubError(502, f"Claude couldn't answer ({exc.status_code}).") from exc
        except anthropic.APIConnectionError as exc:
            del messages[start:]
            raise HubError(504, "Couldn't reach Claude. Check the Mac's internet connection.") from exc
        return reply

    def _gemini(self) -> Any:
        key = (self.hub.secrets.get("gemini") or {}).get("api_key")
        if not key:
            raise HubError(409, "Add a Gemini key in Settings → Ask Home first.")
        if self._gemini_factory:
            return self._gemini_factory(key)
        from google import genai
        return genai.Client(api_key=key)

    async def _gemini_loop(self, contents: list, prompt: str, turn: dict, model: str = GEMINI_MODEL) -> str:
        """The same tools through Gemini's function calling. The model's turns are kept exactly as returned
        (they carry thought signatures Gemini needs to see again)."""
        from google.genai import errors, types
        client = self._gemini()
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"])
                 for t in TOOLS]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM, tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        start = len(contents)
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
        try:
            for _ in range(MAX_ROUNDS):
                response = await client.aio.models.generate_content(model=model, contents=contents, config=config)
                candidate = response.candidates[0] if response.candidates else None
                if candidate is None or candidate.content is None or not candidate.content.parts:
                    reason = getattr(candidate, "finish_reason", None) if candidate else None
                    del contents[start:]
                    return f"Gemini didn't answer{f' ({reason})' if reason else ''}. Try rephrasing."
                contents.append(candidate.content)
                calls = [p.function_call for p in candidate.content.parts if p.function_call]
                if not calls:
                    return "".join(p.text for p in candidate.content.parts if p.text and not p.thought).strip()
                parts = []
                for call in calls:
                    out, is_error = await self._tool_result(call.name, dict(call.args or {}), turn)
                    parts.append(types.Part(function_response=types.FunctionResponse(
                        id=call.id, name=call.name, response={"error": out} if is_error else {"result": out})))
                contents.append(types.Content(role="user", parts=parts))
            return "That took too many steps. Try asking for one thing at a time."
        except errors.APIError as exc:
            del contents[start:]
            raise HubError(*gemini_error(exc, model)) from exc

    async def _openai_loop(self, messages: list, prompt: str, turn: dict, model: str, effort: str = "medium") -> str:
        """The same tools through OpenAI's Chat Completions function calling."""
        import openai
        key = (self.hub.secrets.get("openai") or {}).get("api_key")
        if not key:
            raise HubError(409, "Add an OpenAI key in Settings → Ask Home first.")
        client = self._openai_factory(key) if self._openai_factory else openai.AsyncOpenAI(api_key=key)
        start = len(messages)
        if not messages:
            messages.append({"role": "system", "content": SYSTEM})
        messages.append({"role": "user", "content": prompt})
        try:
            for _ in range(MAX_ROUNDS):
                response = await _openai_create(client, model=model, messages=messages, tools=OPENAI_TOOLS,
                                                reasoning_effort=effort, max_completion_tokens=16000)
                msg = response.choices[0].message
                calls = list(msg.tool_calls or [])
                entry: dict = {"role": "assistant", "content": msg.content}
                if calls:
                    entry["tool_calls"] = [{"id": c.id, "type": "function",
                                            "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls]
                messages.append(entry)
                if not calls:
                    return (msg.content or msg.refusal or "").strip()
                for c in calls:
                    try:
                        args = json.loads(c.function.arguments or "{}")
                    except ValueError:
                        out, is_error = "The arguments weren't valid JSON.", True
                    else:
                        out, is_error = await self._tool_result(c.function.name, args if isinstance(args, dict) else {}, turn)
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": f"Error: {out}" if is_error else out})
            return "That took too many steps. Try asking for one thing at a time."
        except openai.AuthenticationError as exc:
            del messages[start:]
            raise HubError(400, "OpenAI rejected the API key. Check it in Settings → Ask Home.") from exc
        except openai.RateLimitError as exc:
            del messages[start:]
            if "insufficient_quota" in str(exc):
                raise HubError(402, "The OpenAI account has no credit left. Add credit at platform.openai.com → Billing.") from exc
            raise HubError(429, "OpenAI is limiting requests right now. Try again in a minute.") from exc
        except openai.NotFoundError as exc:
            del messages[start:]
            raise HubError(400, f"OpenAI model {model} isn't available to this key. Choose another in Settings → Ask Home.") from exc
        except openai.APIStatusError as exc:
            del messages[start:]
            raise HubError(502, f"OpenAI couldn't answer ({exc.status_code}).") from exc
        except openai.APIConnectionError as exc:
            del messages[start:]
            raise HubError(504, "Couldn't reach OpenAI. Check the Mac's internet connection.") from exc


OPENAI_TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["input_schema"]}} for t in TOOLS]


async def _openai_create(client: Any, **kw: Any) -> Any:
    """Reasoning models take reasoning_effort; older chat models reject it, so retry once without."""
    import openai
    try:
        return await client.chat.completions.create(**kw)
    except openai.BadRequestError as exc:
        if "reasoning_effort" not in str(exc) or "reasoning_effort" not in kw:
            raise
        kw.pop("reasoning_effort")
        return await client.chat.completions.create(**kw)


def gemini_error(exc: Any, model: str) -> tuple[int, str]:
    code = getattr(exc, "code", 0) or 0
    detail = str(getattr(exc, "message", None) or exc)
    if code == 429:
        return 429, ("Gemini's free limit is used up for now (it allows a few requests a minute and a daily total that "
                     "resets at midnight Pacific). Wait, switch Ask Home to Claude in Settings, or turn on billing for the key.")
    if code in (400, 401, 403) and ("API key" in detail or "API_KEY" in detail or code in (401, 403)):
        return 400, "Google rejected the Gemini API key. Check it in Settings → Ask Home."
    if code == 404:
        return 400, f"Gemini model {model} isn't available to this key. Choose another model in Settings → Ask Home."
    if code >= 500:
        return 502, "Gemini had a problem answering. Try again in a moment."
    return 400, f"Gemini said: {detail[:200]}"
