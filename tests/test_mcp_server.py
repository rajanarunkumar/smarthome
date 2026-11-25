import asyncio
import json

import httpx

from hub.mcp_server import OPS, build_server, load_spec, split_args

SPEC = load_spec()
SECRET_PATHS = ("/api/auth", "/account", "/api/settings", "/api/backup/key", "/api/nest/", "/api/hue/pair")


def test_every_tool_matches_an_endpoint_and_none_touch_secrets():
    for op in OPS:
        assert op.method.lower() in SPEC["paths"][op.path], op.name
        assert not any(s in op.path for s in SECRET_PATHS), op.name
        assert not (op.method == "PUT" and op.path == "/api/backup"), op.name
    assert len({op.name for op in OPS}) == len(OPS)


def test_split_args_fills_path_and_body():
    op = next(o for o in OPS if o.name == "control_device")
    path, query, body = split_args(op, SPEC, {"device_id": "kasa:ab/c", "on": False})
    assert path == "/api/devices/kasa:ab%2Fc/state" and query == {} and body == {"on": False}
    op = next(o for o in OPS if o.name == "electricity_insights")
    assert split_args(op, SPEC, {"days": 14}) == ("/api/energy", {"days": 14}, None)


def test_tools_over_a_real_mcp_session():
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.content.decode() or None,
                     request.headers.get("authorization")))
        if request.url.path == "/api/devices/hue:1/state":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(422, json={"detail": [{"msg": "Value error, Too bright"}]})

    async def run():
        client = httpx.AsyncClient(base_url="http://hub", headers={"Authorization": "Bearer t"},
                                   transport=httpx.MockTransport(handler))
        server = build_server(SPEC, client)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            task = asyncio.create_task(server.run(*server_streams, server.create_initialization_options()))
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                assert len(tools) == len(OPS)
                assert tools["list_devices"].annotations.read_only_hint
                assert tools["delete_schedule"].annotations.destructive_hint
                ok = await session.call_tool("control_device", {"device_id": "hue:1", "on": True, "brightness": 40})
                bad = await session.call_tool("control_device", {"device_id": "hue:2", "brightness": 40})
            task.cancel()
        await client.aclose()
        return ok, bad

    ok, bad = asyncio.run(run())
    assert not ok.is_error and json.loads(ok.content[0].text) == {"ok": True}
    assert bad.is_error and "Too bright" in bad.content[0].text
    assert seen[0] == ("POST", "/api/devices/hue:1/state", '{"on":true,"brightness":40}', "Bearer t")
