"""Chat: Claude's tool calls control devices, never unlock, and setup changes wait for Apply."""

import asyncio
from types import SimpleNamespace

import pytest

from hub.chat import Chat
from hub.models import Device, HubError, Schedule


def dev(id, name, kind="light", room_id=1, **kw):
    return Device(id=id, brand=id.split(":")[0], name=name, kind=kind, room_id=room_id, capabilities=kw.pop("capabilities", ["onoff"]), **kw)


class FakeHub:
    def __init__(self, key="sk-test"):
        self.secrets = SimpleNamespace(get=lambda k: key if k == "anthropic_api_key" else None)
        self.devs = [dev("kasa:1", "Plug 1", "plug"), dev("hue:2", "Hue color lamp 2", on=True, brightness=80),
                     dev("schlage:3", "Front door", "lock", room_id=None, on=True, capabilities=["lock"])]
        self.last_devices = {d.id: d for d in self.devs}
        self.calls = []
        self.rooms = [{"id": 1, "name": "Living Room"}]
        hub = self

        class Store:
            def rooms(self):
                return hub.rooms

            def room_by_name(self, name):
                return next((r for r in hub.rooms if r["name"].lower() == name.lower()), None)

            def add_room(self, name):
                hub.rooms.append({"id": len(hub.rooms) + 1, "name": name})
                return hub.rooms[-1]

            def set_device_room(self, device_id, room_id):
                hub.calls.append(("room", device_id, room_id))
        self.store = Store()
        self.schedule_cache = SimpleNamespace(invalidate_all=lambda: None)
        self.schedules = [Schedule(id="hub:s1", brand="hub", runs_on="hub", origin="hub", name="Porch on", device_ids=["kasa:1"],
                                   device_names=["Plug 1"], days=["mon"], action={"on": True},
                                   trigger={"type": "time", "time": "18:00"})]

    async def devices(self):
        return self.devs, []

    async def named_schedules(self, fresh=False):
        return self.schedules, []

    async def set_state(self, device_id, state):
        self.calls.append(("state", device_id, state.model_dump(exclude_none=True, exclude_unset=True)))

    async def rename_device(self, device_id, name):
        self.calls.append(("rename", device_id, name))

    async def create_schedules(self, spec):
        self.calls.append(("schedule", spec.name, spec.device_ids, spec.trigger.type))
        return []

    def adapter_for(self, item_id):
        hub = self

        class A:
            async def delete_schedule(self, sid):
                hub.calls.append(("delete", sid))

            async def set_schedule_enabled(self, sid, enabled):
                hub.calls.append(("enabled", sid, enabled))
        return A()


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(name, args, id="t1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=args)


class ScriptedClaude:
    """Replies in order; records each request."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        content = self.replies.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop, model="claude-opus-5-5")


def chat_with(replies, hub=None):
    hub = hub or FakeHub()
    claude = ScriptedClaude(replies)
    return Chat(hub, client_factory=lambda key: claude), hub, claude


def test_turning_things_off_runs_straight_away():
    chat, hub, claude = chat_with([
        [tool("list_devices", {})],
        [tool("control_device", {"device_id": "hue:2", "state": {"on": False}}, id="t2")],
        [text("Turned off the lamp.")],
    ])
    out = asyncio.run(chat.send("turn off the lamp"))
    assert out["reply"] == "Turned off the lamp." and out["plan"] is None
    assert hub.calls == [("state", "hue:2", {"on": False})]
    assert out["done"] == [{"device_id": "hue:2", "name": "Hue color lamp 2", "state": {"on": False}}]
    req = claude.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default" and "thinking" not in req
    assert req["messages"][0]["content"].startswith("[Now: ")
    listed = claude.requests[1]["messages"][-1]["content"][0]
    assert listed["type"] == "tool_result" and '"Living Room"' in listed["content"] and not listed["is_error"]


def test_chat_can_lock_but_never_unlock():
    chat, hub, claude = chat_with([
        [tool("control_device", {"device_id": "schlage:3", "state": {"locked": False}})],
        [text("I can't unlock from chat.")],
    ])
    asyncio.run(chat.send("unlock the front door"))
    result = claude.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "Unlocking isn't possible" in result["content"] and hub.calls == []


def test_bad_tool_input_goes_back_to_claude_as_an_error():
    chat, hub, claude = chat_with([
        [tool("control_device", {"device_id": "hue:2", "state": {"brightness": 500}}),
         tool("control_device", {"device_id": "hue:99", "state": {"on": True}}, id="t2")],
        [text("Sorry.")],
    ])
    asyncio.run(chat.send("make it really bright"))
    results = claude.requests[1]["messages"][-1]["content"]
    assert [r["is_error"] for r in results] == [True, True] and "No device with id" in results[1]["content"]
    assert hub.calls == []


def test_setup_changes_wait_for_apply_and_only_ticked_ones_happen():
    chat, hub, claude = chat_with([
        [tool("propose_changes", {"summary": "Clearer names and a porch schedule", "changes": [
            {"type": "rename", "device_id": "kasa:1", "name": "Porch Light", "reason": "Name it after what's plugged in"},
            {"type": "move", "device_id": "schlage:3", "room": "Entry"},
            {"type": "create_schedule", "name": "Porch at dusk", "device_ids": ["kasa:1"], "on": True, "trigger": "sunset",
             "offset_min": -15, "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
            {"type": "pause_schedule", "schedule_id": "hub:s1"},
            {"type": "rename", "device_id": "nope:1", "name": "Ghost"},
        ]})],
        [text("Here's a plan to review.")],
        [text("Noted.")],
    ])
    out = asyncio.run(chat.send("tidy up my names"))
    plan = out["plan"]
    assert hub.calls == []                                     # nothing happens before Apply
    assert [c["label"] for c in plan["changes"]] == [
        "Rename “Plug 1” to “Porch Light”", "Move “Front door” to Entry",
        "New schedule “Porch at dusk”: Plug 1 on at sunset -15 min, every day", "Pause schedule “Porch on”"]
    assert len(plan["problems"]) == 1 and "known device_id" in plan["problems"][0]

    result = asyncio.run(chat.apply_plan(plan["id"], only=[0, 1, 2]))
    assert all(r["ok"] for r in result["results"])
    assert hub.calls == [("rename", "kasa:1", "Porch Light"), ("room", "schlage:3", 2),
                         ("schedule", "Porch at dusk", ["kasa:1"], "sunset")]
    assert hub.rooms[-1]["name"] == "Entry"
    with pytest.raises(HubError) as err:
        asyncio.run(chat.apply_plan(plan["id"]))
    assert err.value.status == 409

    asyncio.run(chat.send("thanks", out["conversation_id"]))   # Claude hears what was applied
    assert "applied plan" in claude.requests[-1]["messages"][-1]["content"]


def test_a_plan_with_nothing_valid_is_an_error_claude_sees():
    chat, hub, claude = chat_with([
        [tool("propose_changes", {"summary": "x", "changes": [{"type": "rename", "device_id": "nope", "name": "A"}]})],
        [text("That device doesn't exist.")],
    ])
    out = asyncio.run(chat.send("rename it"))
    assert out["plan"] is None and claude.requests[1]["messages"][-1]["content"][0]["is_error"]


def test_no_api_key_says_where_to_add_one():
    chat = Chat(FakeHub(key=None), client_factory=lambda key: None)
    with pytest.raises(HubError) as err:
        asyncio.run(chat.send("hello"))
    assert err.value.status == 409 and "Photo analysis" in err.value.message


def test_conversation_continues_and_history_is_only_appended():
    chat, hub, claude = chat_with([[text("Hi.")], [text("Still here.")]])
    first = asyncio.run(chat.send("hello"))
    asyncio.run(chat.send("again", first["conversation_id"]))
    a, b = claude.requests
    assert b["messages"][:len(a["messages"])] == a["messages"]   # earlier turns sent back unchanged
