import asyncio
import json
import os

import httpx

from hub.mcp_clients import ACTIVE_S, McpClients
from hub.mcp_server import build_server, load_spec


async def _whois(ip):
    return {"100.64.0.2": "iphone"}.get(ip)


def test_http_clients_are_named_counted_and_expire():
    clients = McpClients(whois=_whois)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "claude-code", "version": "2.1"}}}
    clients.note_http("100.64.0.2", "claude-code/2.1", "someone@example.com", json.dumps(init).encode())
    call = [{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "electricity_insights"}}]
    clients.note_http("100.64.0.2", "claude-code/2.1", "someone@example.com", json.dumps(call).encode())
    clients.note_http("127.0.0.1", "node", None, b"not json")
    clients.note_http("bogus; rm -rf /", "x", None, b"")   # never reaches `tailscale whois`

    report = asyncio.run(clients.report())
    by_device = {c["device"]: c for c in report["clients"]}
    phone = by_device["iphone"]
    assert (phone["client"], phone["version"], phone["transport"]) == ("claude-code", "2.1", "http")
    assert phone["requests"] == 2 and phone["tool_calls"] == 1 and phone["last_tool"] == "electricity_insights"
    assert phone["connects"] == 1 and phone["connected"]
    assert by_device["this Mac"]["client"] is None
    assert report["connected"] == 3

    for c in clients._clients.values():
        c["last_seen"] -= ACTIVE_S + 1
    assert asyncio.run(clients.report())["connected"] == 0


def test_stdio_clients_follow_their_process():
    clients = McpClients(whois=_whois)
    clients.note_stdio(json.dumps({"pid": os.getpid(), "name": "claude-code", "version": "2.1"}))
    clients.note_stdio(json.dumps({"pid": os.getpid(), "name": "claude-code", "version": "2.1", "tool": "bills"}))
    clients.note_stdio(json.dumps({"pid": 2 ** 22 + 12345, "name": "gone"}))   # no such process
    clients.note_stdio("garbage")
    report = asyncio.run(clients.report())
    live = next(c for c in report["clients"] if c["pid"] == os.getpid())
    assert live["connected"] and live["device"] == "this Mac" and live["tool_calls"] == 1 and live["connects"] == 1
    assert report["connected"] == 1 and len(report["clients"]) == 2


def test_stdio_server_tells_the_hub_who_it_serves():
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams
    from mcp import types

    headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append((request.url.path, request.headers.get("x-mcp-client")))
        return httpx.Response(200, json={"ok": True})

    async def run():
        client = httpx.AsyncClient(base_url="http://hub", transport=httpx.MockTransport(handler))
        server = build_server(load_spec(), client, identify=True)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            task = asyncio.create_task(server.run(*server_streams, server.create_initialization_options()))
            async with ClientSession(*client_streams, client_info=types.Implementation(name="tester", version="9")) as s:
                await s.initialize()
                await s.list_tools()
                await s.call_tool("bills", {})
            task.cancel()
        await client.aclose()

    asyncio.run(run())
    (hello_path, hello), (call_path, call) = headers
    assert hello_path == "/api/auth/me" and call_path == "/api/bills"
    assert json.loads(hello) == {"pid": os.getpid(), "name": "tester", "version": "9"}
    assert json.loads(call)["tool"] == "bills"
