"""MCP server for the smart home hub: lets Claude Desktop, Claude Code or any MCP client run the home.

It runs on the Mac (stdio) and calls the hub's own HTTP API at 127.0.0.1:8420 with the API token in
~/.smarthome/secrets.json, so everything the app can do goes through the same checks, caches and logs.

Every action you can take on the home is a tool: devices (lights, plugs, TV, speakers, vacuum, purifier,
pool pump, sprinklers, thermostats, locks), rooms, scenes, schedules, irrigation programs, maintenance
logs, prices and settings for the analyses, plus read-only tools for every insight (electricity, bills
by time of day, pool, HVAC, water). Signing in, passwords, API keys and linking accounts are left out on
purpose: those stay in the app.

Tool inputs come from the hub's own request models (its OpenAPI schema), so they can't drift from the API.

Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):
    {"mcpServers": {"smarthome": {"command": "/path/to/smarthome/.venv/bin/python",
                                  "args": ["-m", "hub.mcp_server"], "cwd": "/path/to/smarthome"}}}
Claude Code:  claude mcp add smarthome -- /path/to/smarthome/.venv/bin/python -m hub.mcp_server
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

HUB_URL = os.environ.get("SMARTHOME_URL", "http://127.0.0.1:8420")


@dataclass(frozen=True)
class Op:
    name: str
    method: str
    path: str
    description: str


# Everything the home can do. Left out on purpose: sign-in, passkeys, app tokens, account connect/disconnect,
# API keys and backup keys (secrets belong in the app), and uploading photo files (binary).
OPS: list[Op] = [
    # Home: what's there
    Op("get_status", "GET", "/api/status", "Hub status: which brands are connected, online and their errors."),
    Op("list_devices", "GET", "/api/devices", "Every device with its id, kind, room, on/off, brightness, temperatures, lock state and actions. Use the ids with the other tools."),
    Op("list_rooms", "GET", "/api/rooms", "Rooms and the devices in each."),
    Op("list_scenes", "GET", "/api/scenes", "Hue scenes by room."),
    Op("list_schedules", "GET", "/api/schedules", "Every schedule (on the device, the bridge or the hub), with ids."),
    # Devices
    Op("control_device", "POST", "/api/devices/{device_id}/state",
       "Change a device: on/off, brightness 1-100, color (#RRGGBB), white temperature (mirek), volume, mute, playback, "
       "TV input (source), appliance action, fan level, lock (locked true locks, false UNLOCKS a door), sprinkler zone and "
       "minutes or rain delay, thermostat mode, heat_f/cool_f set temperatures, eco, fan_minutes. Send only the fields to change."),
    Op("update_device", "PATCH", "/api/devices/{device_id}", "Rename a device or move it to a room (room_id)."),
    Op("set_device_watts", "PUT", "/api/energy/watts/{device_id}", "Tell the electricity analysis how many watts a device draws."),
    # Rooms
    Op("add_room", "POST", "/api/rooms", "Create a room."),
    Op("rename_room", "PATCH", "/api/rooms/{room_id}", "Rename a room."),
    Op("delete_room", "DELETE", "/api/rooms/{room_id}", "Delete a room (its devices become unassigned)."),
    Op("control_room", "POST", "/api/rooms/{room_id}/state", "Change every device in a room at once, e.g. all off, or brightness."),
    # Scenes and schedules
    Op("activate_scene", "POST", "/api/scenes/{scene_id}/activate", "Turn on a Hue scene (dynamic=true for a moving scene)."),
    Op("create_schedule", "POST", "/api/schedules", "Create a schedule for a device (saved on the plug or bridge when it can, otherwise run by the hub)."),
    Op("enable_schedule", "PATCH", "/api/schedules/{schedule_id}", "Turn a schedule on or off (enabled)."),
    Op("delete_schedule", "DELETE", "/api/schedules/{schedule_id}", "Delete a schedule."),
    # Sprinklers
    Op("list_irrigation_programs", "GET", "/api/irrigation/programs", "Sprinkler programs run by the hub."),
    Op("add_irrigation_program", "POST", "/api/irrigation/programs", "Add a sprinkler program."),
    Op("edit_irrigation_program", "PUT", "/api/irrigation/programs/{program_id}", "Change a sprinkler program."),
    Op("delete_irrigation_program", "DELETE", "/api/irrigation/programs/{program_id}", "Delete a sprinkler program."),
    Op("run_irrigation_program", "POST", "/api/irrigation/programs/{program_id}/run",
       "Run a sprinkler program now (smart=true skips or shortens zones that don't need water)."),
    Op("rename_sprinkler_zone", "PUT", "/api/rainbird/zones/{zone}", "Name a sprinkler zone."),
    Op("set_sprinkler_zone_schedule", "PUT", "/api/rainbird/zones/{zone}/schedule", "Set one zone's watering schedule."),
    Op("list_flow_tests", "GET", "/api/rainbird/flow-tests", "Planned and measured sprinkler flow tests (gallons per minute per zone)."),
    Op("plan_flow_tests", "POST", "/api/rainbird/flow-tests", "Plan flow tests to measure each zone's water use."),
    Op("cancel_flow_tests", "DELETE", "/api/rainbird/flow-tests", "Cancel planned flow tests."),
    Op("list_zone_photos", "GET", "/api/rainbird/zones/{zone}/photos", "Lawn photos for a zone with their green share and assessment."),
    Op("assess_photo", "POST", "/api/photos/{photo_id}/assess", "Assess a lawn photo again."),
    Op("delete_photo", "DELETE", "/api/photos/{photo_id}", "Delete a lawn photo."),
    # Ring
    Op("ring_events", "GET", "/api/ring/{device_id}/events", "Recent Ring doorbell and camera events (motion, rings)."),
    # Insights
    Op("water_insights", "GET", "/api/insights", "Water and sprinkler analysis: use by day, zones, leaks, advice."),
    Op("electricity_insights", "GET", "/api/energy", "Electricity analysis: kWh by day, devices' power, always-on load, spikes, heating and cooling share."),
    Op("time_of_use", "GET", "/api/tou", "Free-night vs paid-hour use, the average day by hour, and cost advice."),
    Op("bills", "GET", "/api/bills", "TXU bills: kWh, free share, energy, delivery and totals, with advice."),
    Op("bills_by_time_of_day", "GET", "/api/reconcile",
       "Each bill split into night (free), morning, afternoon and evening, with energy, delivery and fees, the seasonal extra, "
       "and savings against the same period last year toward the monthly goal."),
    Op("pool_insights", "GET", "/api/pool", "Pool pump: live flow and power, filter health and what to do (skimmer, backwash, filter, water level), programs and cost."),
    Op("hvac_insights", "GET", "/api/hvac", "Heating and cooling: run time and dollars in free and paid hours, health and what to do, by thermostat."),
    Op("list_appliance_watts", "GET", "/api/energy/watts", "Watts known or entered per device."),
    Op("add_always_on_appliance", "POST", "/api/energy/appliances", "Add an always-on appliance (fridge, freezer…) for the electricity analysis."),
    Op("update_always_on_appliance", "PUT", "/api/energy/appliances/{load_id}", "Change an always-on appliance."),
    Op("delete_always_on_appliance", "DELETE", "/api/energy/appliances/{load_id}", "Remove an always-on appliance."),
    Op("log_pool_maintenance", "POST", "/api/pool/maintenance", "Log pool work done: skimmer, backwash, filter_clean or water_fill. The hub then measures its effect."),
    Op("delete_pool_maintenance", "DELETE", "/api/pool/maintenance/{log_id}", "Remove a pool maintenance entry."),
    Op("log_hvac_maintenance", "POST", "/api/hvac/maintenance", "Log HVAC work done: filter, coil, service or schedule. The hub then measures its effect."),
    Op("delete_hvac_maintenance", "DELETE", "/api/hvac/maintenance/{log_id}", "Remove an HVAC maintenance entry."),
    Op("get_tariff", "GET", "/api/tariff", "Electricity plan: free hours, prices from the latest bill, savings goal."),
    Op("set_tariff", "PUT", "/api/tariff", "Change the free hours, prices or the monthly savings goal (savings_target)."),
    Op("set_location", "PUT", "/api/location", "Set the home's city for weather (e.g. 'McKinney, TX')."),
    Op("refresh_insights", "POST", "/api/insights/refresh", "Recompute the insights now."),
    Op("fetch_history", "POST", "/api/backfill", "Fetch the last 4 weeks of water, electricity, weather and sprinkler history."),
    # Hub
    Op("storage", "GET", "/api/storage", "What the hub stores and how big it is."),
    Op("backup_status", "GET", "/api/backup", "Backup settings and the last backups."),
    Op("backup_now", "POST", "/api/backup/run", "Back up the hub's data now."),
    Op("hub_telemetry", "GET", "/api/admin/telemetry", "Hub health: request times, background jobs, memory."),
    Op("hub_logs", "GET", "/api/admin/logs", "Recent hub log entries (level WARNING by default)."),
    Op("hub_profile", "POST", "/api/admin/profile", "Sample what's using the hub's CPU for a few seconds."),
    Op("refresh_device_cache", "POST", "/api/admin/cache/refresh", "Re-read devices from the brands now."),
    Op("restart_hub", "POST", "/api/admin/restart", "Restart the hub (back in about 10 seconds)."),
]


def _resolve(schema: Any, spec: dict) -> Any:
    """Inline $refs so each tool's input schema stands on its own."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return _resolve(spec["components"]["schemas"][name], spec)
        return {k: _resolve(v, spec) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve(v, spec) for v in schema]
    return schema


def input_schema(op: Op, spec: dict) -> dict:
    """Path and query parameters plus the request body's fields, as one flat JSON schema."""
    operation = spec["paths"][op.path][op.method.lower()]
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in operation.get("parameters", []):
        props[p["name"]] = {k: v for k, v in _resolve(p.get("schema", {}), spec).items() if k != "title"}
        if p.get("description"):
            props[p["name"]]["description"] = p["description"]
        if p.get("required"):
            required.append(p["name"])
    body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    if body:
        body = _resolve(body, spec)
        for name, s in body.get("properties", {}).items():
            props[name] = {k: v for k, v in s.items() if k != "title"}
        required += [r for r in body.get("required", []) if r not in required]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def split_args(op: Op, spec: dict, args: dict) -> tuple[str, dict, dict | None]:
    """Tool arguments -> (path with ids filled in, query params, JSON body)."""
    operation = spec["paths"][op.path][op.method.lower()]
    path_names = set(re.findall(r"{(\w+)}", op.path))
    query_names = {p["name"] for p in operation.get("parameters", []) if p.get("in") == "query"}
    path = op.path
    for name in path_names:
        if name not in args:
            raise ValueError(f"{name} is required")
        path = path.replace("{" + name + "}", quote(str(args[name]), safe=":@"))
    query = {k: v for k, v in args.items() if k in query_names and v is not None}
    has_body = "requestBody" in operation
    body = {k: v for k, v in args.items() if k not in path_names and k not in query_names} if has_body else None
    return path, query, body


def token() -> str:
    if os.environ.get("SMARTHOME_TOKEN"):
        return os.environ["SMARTHOME_TOKEN"]
    from .config import data_dir
    return json.loads((data_dir() / "secrets.json").read_text())["access_token"]


def load_spec() -> dict:
    """The hub's request models, from the code itself (no network, no secrets)."""
    from .app import app
    return app.openapi()


def who(ctx: Any, tool: str | None = None) -> dict[str, str]:
    """X-MCP-Client header: tells the hub which client this stdio server serves (Admin → MCP clients)."""
    try:
        info = ctx.session.client_params.client_info
        name, version = info.name, info.version
    except AttributeError:
        name = version = None
    data = {"pid": os.getpid(), "name": name, "version": version}
    if tool:
        data["tool"] = tool
    return {"X-MCP-Client": json.dumps(data, ensure_ascii=True)}


def build_server(spec: dict, client: httpx.AsyncClient, identify: bool = False):
    """identify: send X-MCP-Client with each call (the stdio server; /mcp notes its callers itself)."""
    from mcp import types
    from mcp.server.lowlevel import Server

    ops = {op.name: op for op in OPS if op.path in spec["paths"] and op.method.lower() in spec["paths"][op.path]}
    tools = [types.Tool(name=op.name, description=op.description, inputSchema=input_schema(op, spec),
                        annotations=types.ToolAnnotations(readOnlyHint=op.method == "GET",
                                                          destructiveHint=op.method == "DELETE" or op.name == "restart_hub",
                                                          openWorldHint=False))
             for op in ops.values()]

    async def list_tools(ctx, params) -> types.ListToolsResult:
        if identify:   # clients list tools as soon as they connect: let Admin see it
            try:
                await client.get("/api/auth/me", headers=who(ctx))
            except httpx.HTTPError:
                pass
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        def result(text: str, error: bool = False) -> types.CallToolResult:
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)

        op = ops.get(params.name)
        if op is None:
            return result(f"Unknown tool {params.name}", True)
        try:
            path, query, body = split_args(op, spec, dict(params.arguments or {}))
            r = await client.request(op.method, path, params=query, json=body,
                                     headers=who(ctx, op.name) if identify else None)
        except (ValueError, httpx.HTTPError) as exc:
            return result(f"Couldn't reach the hub: {exc}" if isinstance(exc, httpx.HTTPError) else str(exc), True)
        try:
            data = r.json()
        except ValueError:
            return result(r.text[:2000] if r.status_code < 400 else f"The hub said no ({r.status_code}): {r.text[:300]}",
                          r.status_code >= 400)
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else data
            if isinstance(detail, list):
                detail = "; ".join(str(d.get("msg", d)).removeprefix("Value error, ") for d in detail)
            return result(f"The hub said no ({r.status_code}): {detail}", True)
        return result(json.dumps(data, separators=(",", ":"), default=str))

    return Server("smarthome", version="1.0", title="Smart home hub",
                  instructions="Controls and reads the house through its hub. Call list_devices first to get device ids. "
                               "Unlocking doors, deleting schedules and restarting the hub change the house: confirm with the user first.",
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def main() -> None:
    from mcp.server.stdio import stdio_server

    spec = load_spec()
    async with httpx.AsyncClient(base_url=HUB_URL, timeout=90, headers={"Authorization": f"Bearer {token()}"}) as client:
        server = build_server(spec, client, identify=True)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
