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
    assert req["model"] == "claude-haiku-5-5" and req["output_config"] == {"effort": "low"}   # a plain command: quick model
    assert "fallbacks" not in req and "thinking" not in req                                    # Haiku has no server-side fallback
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


# ---------------- Gemini ----------------
from google.genai import errors as genai_errors  # noqa: E402
from google.genai import types as gt  # noqa: E402


class GeminiHub(FakeHub):
    def __init__(self, gemini=True, claude=False, choice=None):
        super().__init__(key="sk-test" if claude else None)
        data = {"gemini": {"api_key": "AIza-test-key-0000000000"} if gemini else None, "chat": {"provider": choice} if choice else None,
                "anthropic_api_key": "sk-test" if claude else None}
        self.secrets = SimpleNamespace(get=lambda k: data.get(k))


class ScriptedGemini:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self.generate))

    async def generate(self, model, contents, config):
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(candidates=[SimpleNamespace(content=gt.Content(role="model", parts=reply), finish_reason="STOP")])


def call(name, args, id="g1"):
    return gt.Part(function_call=gt.FunctionCall(id=id, name=name, args=args), thought_signature=b"sig")


def test_provider_prefers_the_choice_then_gemini_then_claude():
    assert Chat(GeminiHub(gemini=True, claude=True)).provider() == "gemini"
    assert Chat(GeminiHub(gemini=True, claude=True, choice="claude")).provider() == "claude"
    assert Chat(GeminiHub(gemini=False, claude=True, choice="gemini")).provider() == "claude"   # chosen one has no key
    assert Chat(GeminiHub(gemini=False, claude=False)).provider() is None


def test_gemini_controls_devices_with_the_same_tools():
    hub = GeminiHub()
    gem = ScriptedGemini([
        [call("list_devices", {})],
        [call("control_device", {"device_id": "hue:2", "state": {"brightness": 30}}, id="g2")],
        [gt.Part(text="Dimmed the lamp to 30%.")],
    ])
    chat = Chat(hub, gemini_factory=lambda key: gem)
    out = asyncio.run(chat.send("dim the lamp"))
    assert out["reply"] == "Dimmed the lamp to 30%." and out["provider"] == "gemini"
    assert hub.calls == [("state", "hue:2", {"brightness": 30})]
    req = gem.requests[2]
    assert req["model"] == "gemini-flash-lite-latest"
    assert req["config"].automatic_function_calling.disable is True
    names = [d.name for d in req["config"].tools[0].function_declarations]
    assert names == ["list_devices", "list_schedules", "control_device", "propose_changes"]
    model_turn, answer = req["contents"][-2], req["contents"][-1]
    assert model_turn.parts[0].thought_signature == b"sig"            # the model's turn is sent back as returned
    fr = answer.parts[0].function_response
    assert fr.id == "g2" and fr.response == {"result": "Done."}


def test_gemini_never_unlocks_either():
    hub = GeminiHub()
    gem = ScriptedGemini([[call("control_device", {"device_id": "schlage:3", "state": {"locked": False}})],
                          [gt.Part(text="You'll need to unlock it in the app.")]])
    asyncio.run(Chat(hub, gemini_factory=lambda key: gem).send("unlock the door"))
    fr = gem.requests[1]["contents"][-1].parts[0].function_response
    assert "Unlocking isn't possible" in fr.response["error"] and hub.calls == []


def test_gemini_free_limit_is_explained_and_the_turn_is_dropped():
    hub = GeminiHub()
    gem = ScriptedGemini([genai_errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}}),
                          [gt.Part(text="Hi.")]])
    chat = Chat(hub, gemini_factory=lambda key: gem)
    with pytest.raises(HubError) as err:
        asyncio.run(chat.send("hello"))
    assert err.value.status == 429 and "free limit" in err.value.message
    out = asyncio.run(chat.send("hello again"))
    assert len(gem.requests[1]["contents"]) == 1 and out["reply"] == "Hi."   # the failed turn wasn't kept


def test_switching_provider_carries_a_recap_over():
    hub = GeminiHub(gemini=True, claude=True)
    gem = ScriptedGemini([[gt.Part(text="Gemini here.")]])
    claude = ScriptedClaude([[text("Claude here.")]])
    chat = Chat(hub, client_factory=lambda key: claude, gemini_factory=lambda key: gem)
    first = asyncio.run(chat.send("hi"))
    hub.secrets = SimpleNamespace(get=lambda k: {"gemini": {"api_key": "AIza-x"}, "chat": {"provider": "claude"}, "anthropic_api_key": "sk"}.get(k))
    second = asyncio.run(chat.send("hi", first["conversation_id"]))
    assert second["provider"] == "claude" and second["conversation_id"] == first["conversation_id"]
    [only] = claude.requests[0]["messages"]                      # Gemini's turns aren't sent to Claude...
    assert "Earlier — you: Gemini here." in only["content"]      # ...but a plain recap is


def fake_gemini_models(hub, monkeypatch, ids=("gemini-flash-lite-latest", "gemini-flash-latest")):
    async def connect(key):
        hub.secrets.set("gemini", {"api_key": key, "models": list(ids), "names": {}})
        return list(ids)
    monkeypatch.setattr(hub.chat, "connect_gemini", connect)


def test_chat_settings_route(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        fake_gemini_models(hub, monkeypatch)
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        assert client.get("/api/status", headers=token).json()["chat"]["provider"] is None
        assert client.put("/api/settings/chat", json={"gemini_key": "bad key!"}, headers=token).status_code == 422
        r = client.put("/api/settings/chat", json={"gemini_key": "AIzaSyTestKey_0123456789abcdef", "provider": "gemini"}, headers=token)
        assert r.json()["provider"] == "gemini"
        chat = client.get("/api/status", headers=token).json()["chat"]
        assert {k: chat[k] for k in ("provider", "gemini", "model", "choice")} == {
            "provider": "gemini", "gemini": True, "model": "gemini-flash-lite-latest", "choice": "gemini"}
        assert chat["models"] == {"quick": "gemini:gemini-flash-lite-latest", "thorough": "gemini:gemini-flash-latest"}
        client.put("/api/settings/chat", json={"model": "gemini-flash-latest"}, headers=token)
        assert hub.secrets.get("gemini")["model"] == "gemini-flash-latest" and hub.secrets.get("gemini")["api_key"].startswith("AIza")
        client.delete("/api/settings/gemini", headers=token)
        assert client.get("/api/status", headers=token).json()["chat"]["provider"] is None


def test_reviews_go_to_the_thorough_model_and_commands_to_the_quick_one():
    hub = GeminiHub(gemini=True, claude=True)
    gem = ScriptedGemini([[gt.Part(text="Done.")]])
    claude = ScriptedClaude([[text("Here's a review.")], [text("Ok.")]])
    chat = Chat(hub, client_factory=lambda key: claude, gemini_factory=lambda key: gem)
    quick = asyncio.run(chat.send("turn on the porch light"))
    review = asyncio.run(chat.send("review my device names", quick["conversation_id"]))
    assert (quick["mode"], quick["model"]) == ("quick", "gemini:gemini-flash-lite-latest")
    assert (review["mode"], review["model"], review["model_label"]) == ("thorough", "claude:claude-opus-5-5", "Claude Opus 5.5")
    assert claude.requests[0]["fallbacks"] == "default" and claude.requests[0]["output_config"] == {"effort": "medium"}
    forced = asyncio.run(chat.send("turn on the porch light", quick["conversation_id"], mode="thorough"))
    assert forced["model"] == "claude:claude-opus-5-5"


def test_saved_choices_and_one_off_model_override():
    data = {"gemini": {"api_key": "AIza-x"}, "anthropic_api_key": "sk",
            "chat": {"models": {"quick": "claude:claude-sonnet-5-5", "thorough": "gemini:gemini-flash-latest"}}}
    hub = GeminiHub()
    hub.secrets = SimpleNamespace(get=lambda k: data.get(k))
    chat = Chat(hub)
    assert chat.model_for("quick") == "claude:claude-sonnet-5-5" and chat.model_for("thorough") == "gemini:gemini-flash-latest"
    assert [m["id"] for m in chat.choices()["available"]][:2] == ["gemini:gemini-flash-lite-latest", "gemini:gemini-flash-latest"]
    data["anthropic_api_key"] = None                                     # the chosen model's key was removed
    assert chat.model_for("quick") == "gemini:gemini-flash-lite-latest"
    assert all(m["id"].startswith("gemini:") for m in chat.choices()["available"])
    with pytest.raises(HubError) as err:
        asyncio.run(chat.send("hi", model="claude:claude-opus-5-5"))       # one-off pick without a key
    assert err.value.status == 409
    with pytest.raises(HubError):
        asyncio.run(chat.send("hi", model="openai:gpt-9"))


def test_model_settings_route(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        fake_gemini_models(hub, monkeypatch)
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        client.put("/api/settings/chat", json={"gemini_key": "AIzaSyTestKey_0123456789abcdef"}, headers=token)
        hub.secrets.set("anthropic_api_key", "sk-ant-test")
        r = client.put("/api/settings/chat", json={"quick": "claude:claude-haiku-5-5", "thorough": "claude:claude-sonnet-5-5",
                                                    "photo_model": "claude-sonnet-5-5"}, headers=token).json()
        assert r["models"] == {"quick": "claude:claude-haiku-5-5", "thorough": "claude:claude-sonnet-5-5"}
        assert len(r["available"]) == 5
        st = client.get("/api/status", headers=token).json()["chat"]
        assert st["photo_model"] == "claude-sonnet-5-5" and st["models"]["quick"] == "claude:claude-haiku-5-5"
        assert client.put("/api/settings/chat", json={"quick": "openai:gpt"}, headers=token).status_code == 400
        assert client.put("/api/settings/chat", json={"photo_model": "claude-opus-1"}, headers=token).status_code == 400
        assert client.post("/api/chat", json={"message": "hi", "model": "bad model"}, headers=token).status_code == 422


# ---------------- OpenAI ----------------
import json as _json  # noqa: E402

import openai  # noqa: E402


class OpenAIHub(FakeHub):
    def __init__(self, models=("gpt-9", "gpt-9-mini")):
        super().__init__(key=None)
        self.data = {"openai": {"api_key": "sk-proj-test", "models": list(models)}}
        self.secrets = SimpleNamespace(get=lambda k: self.data.get(k), set=lambda k, v: self.data.__setitem__(k, v))


def oa_call(name, args, id="call_1"):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=_json.dumps(args)))


class ScriptedOpenAI:
    def __init__(self, replies, models=(), reject_effort=False):
        self.replies, self.requests, self._models, self.reject_effort = list(replies), [], list(models), reject_effort
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.models = SimpleNamespace(list=self.list_models)

    async def create(self, **kw):
        if self.reject_effort and "reasoning_effort" in kw:
            self.reject_effort = False
            raise openai.BadRequestError("Unsupported parameter: 'reasoning_effort'",
                                         response=__import__("httpx").Response(400, request=__import__("httpx").Request("POST", "https://x")), body=None)
        self.requests.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        calls, content = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None, tool_calls=calls))])

    def list_models(self):
        async def gen():
            for m in self._models:
                yield m
        return gen()


def test_openai_lists_usable_models_newest_first():
    M = lambda id, t: SimpleNamespace(id=id, created=t)  # noqa: E731
    models = [M("gpt-9", 300), M("gpt-9-mini", 290), M("gpt-9-2026-01-01", 280), M("gpt-image-2", 310), M("o7", 200),
              M("text-embedding-4", 100), M("gpt-9-realtime", 305), M("dall-e-5", 50), M("gpt-8", 150)]
    hub = OpenAIHub(models=())
    hub.data = {}
    chat = Chat(hub, openai_factory=lambda key: ScriptedOpenAI([], models=models))
    ids = asyncio.run(chat.connect_openai("sk-proj-test-key-000000"))
    assert ids == ["gpt-9", "gpt-9-mini", "o7", "gpt-8"] and hub.data["openai"]["api_key"] == "sk-proj-test-key-000000"
    assert chat.model_for("quick") == "openai:gpt-9-mini" and chat.model_for("thorough") == "openai:gpt-9"
    assert [m["label"] for m in chat.choices()["available"]][0] == "OpenAI gpt-9 · paid"


def test_openai_controls_devices_with_the_same_tools():
    hub = OpenAIHub()
    oa = ScriptedOpenAI([([oa_call("list_devices", {})], None),
                         ([oa_call("control_device", {"device_id": "hue:2", "state": {"on": False}}, id="call_2")], None),
                         (None, "Turned off the lamp.")], reject_effort=True)
    chat = Chat(hub, openai_factory=lambda key: oa)
    out = asyncio.run(chat.send("turn off the lamp"))
    assert out["reply"] == "Turned off the lamp." and out["model"] == "openai:gpt-9-mini" and out["model_label"] == "OpenAI gpt-9-mini"
    assert hub.calls == [("state", "hue:2", {"on": False})]
    first = oa.requests[0]
    assert "reasoning_effort" not in first                        # the model rejected it once; retried without
    assert first["messages"][0]["role"] == "system" and first["tools"][0]["function"]["name"] == "list_devices"
    last = oa.requests[-1]["messages"]
    assert last[-2]["tool_calls"][0]["id"] == "call_2" and last[-1] == {"role": "tool", "tool_call_id": "call_2", "content": "Done."}


def test_openai_never_unlocks_and_bad_json_is_reported():
    hub = OpenAIHub()
    bad = SimpleNamespace(id="call_9", function=SimpleNamespace(name="control_device", arguments="{not json"))
    oa = ScriptedOpenAI([([oa_call("control_device", {"device_id": "schlage:3", "state": {"locked": False}}), bad], None),
                         (None, "Can't do that from chat.")])
    asyncio.run(Chat(hub, openai_factory=lambda key: oa).send("unlock the door"))
    msgs = oa.requests[1]["messages"]
    assert "Unlocking isn't possible" in msgs[-2]["content"] and "valid JSON" in msgs[-1]["content"] and hub.calls == []


def test_a_removed_openai_model_falls_back():
    hub = OpenAIHub(models=("gpt-9",))
    hub.data["chat"] = {"models": {"quick": "openai:gpt-8-mini"}}
    assert Chat(hub).model_for("quick") == "openai:gpt-9"


# ---------------- Gemini: the key's own model list ----------------
from hub.chat import gemini_chat_models, pick_gemini  # noqa: E402


def gm(name, actions=("generateContent",), display=None):
    return SimpleNamespace(name=f"models/{name}", supported_actions=list(actions), display_name=display or name)


GOOGLE_MODELS = [gm("gemini-2.5-flash", display="Gemini 2.5 Flash"), gm("gemini-2.5-flash-lite", display="Gemini 2.5 Flash-Lite"),
                 gm("gemini-3.1-flash-lite-preview"), gm("gemini-2.0-flash"), gm("gemini-2.5-pro"),
                 gm("text-embedding-004", ("embedContent",)), gm("gemini-embedding-001", ("embedContent",)),
                 gm("gemini-2.5-flash-image"), gm("gemini-2.5-flash-preview-tts"), gm("gemma-3-27b-it"), gm("imagen-4.0")]


def test_gemini_models_come_from_the_key():
    ids, names = gemini_chat_models(GOOGLE_MODELS)
    assert ids == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-3.1-flash-lite-preview"]
    assert pick_gemini(ids, "lite") == "gemini-2.5-flash-lite" and pick_gemini(ids, "flash") == "gemini-2.5-flash"
    assert names["gemini-2.5-flash-lite"] == "Gemini 2.5 Flash-Lite"


class ListingGemini(ScriptedGemini):
    def __init__(self, replies, models=GOOGLE_MODELS, missing=()):
        super().__init__(replies)
        self._models, self.missing = models, set(missing)
        self.aio.models.list = self.list_models

    async def list_models(self):
        async def gen():
            for m in self._models:
                yield m
        return gen()

    async def generate(self, model, contents, config):
        if model in self.missing:
            raise genai_errors.ClientError(404, {"error": {"code": 404, "message": f"models/{model} is not found", "status": "NOT_FOUND"}})
        return await super().generate(model, contents, config)


def test_an_old_key_learns_its_models_and_an_unavailable_model_is_replaced():
    hub = GeminiHub()
    data = {"gemini": {"api_key": "AIza-old", "model": "gemini-flash-lite-latest"}}
    hub.secrets = SimpleNamespace(get=lambda k: data.get(k), set=lambda k, v: data.__setitem__(k, v))
    gem = ListingGemini([[gt.Part(text="Done.")]], missing={"gemini-flash-lite-latest"})
    chat = Chat(hub, gemini_factory=lambda key: gem)
    out = asyncio.run(chat.send("turn on the porch light"))
    assert data["gemini"]["models"][0] == "gemini-2.5-flash" and "model" not in data["gemini"]   # stale pick dropped
    assert out["model"] == "gemini:gemini-2.5-flash-lite" and gem.requests[0]["model"] == "gemini-2.5-flash-lite"
    labels = {m["id"]: m["label"] for m in chat.choices()["available"]}
    assert labels["gemini:gemini-2.5-flash-lite"].startswith("Gemini 2.5 Flash-Lite · free, most free use")


def test_a_model_that_disappears_mid_use_is_swapped_for_the_same_tier():
    hub = GeminiHub()
    data = {"gemini": {"api_key": "AIza-x", "models": ["gemini-9-flash-lite", "gemini-9-flash"], "names": {}}}
    hub.secrets = SimpleNamespace(get=lambda k: data.get(k), set=lambda k, v: data.__setitem__(k, v))
    gem = ListingGemini([[gt.Part(text="Hi.")]], missing={"gemini-9-flash-lite"})
    out = asyncio.run(Chat(hub, gemini_factory=lambda key: gem).send("hello"))
    assert out["model"] == "gemini:gemini-2.5-flash-lite" and "isn't available to this key" in out["reply"]
    assert data["gemini"]["models"][0] == "gemini-2.5-flash"


def test_a_rejected_gemini_key_says_so():
    hub = GeminiHub()

    class Bad(ListingGemini):
        async def list_models(self):
            raise genai_errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid", "status": "INVALID_ARGUMENT"}})
    with pytest.raises(HubError) as err:
        asyncio.run(Chat(hub, gemini_factory=lambda key: Bad([])).connect_gemini("AIza-bad"))
    assert "rejected" in err.value.message
