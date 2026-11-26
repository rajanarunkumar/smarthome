"""Which MCP clients use the hub (Claude Code, Claude Desktop, mcp-remote…), for Admin → MCP clients.

Two ways in:
- HTTP at /mcp (from any device on the tailnet). It's stateless, so a client is "connected" while it has
  made a request in the last ACTIVE_S seconds. Its name and version come from its `initialize` message;
  the device name comes from `tailscale whois`.
- The stdio server (`python -m hub.mcp_server`) on this Mac. It tells the hub its process id and client
  in an X-MCP-Client header, so it counts as connected for as long as that process is running.

Kept in memory only and starts over when the hub restarts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import time
from typing import Any, Awaitable, Callable

ACTIVE_S = 600            # an HTTP client counts as connected for 10 minutes after its last request
KEEP_S = 7 * 86400        # forget clients not seen for a week
MAX_CLIENTS = 50
TAILSCALE_PATHS = ("/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
                   "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


async def tailscale_name(ip: str) -> str | None:
    """The tailnet device name for an address (e.g. "iphone171"), or None."""
    exe = shutil.which("tailscale") or next((p for p in TAILSCALE_PATHS if os.path.exists(p)), None)
    if not exe:
        return None
    proc = await asyncio.create_subprocess_exec(exe, "whois", "--json", ip, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 3)
    except TimeoutError:
        proc.kill()
        return None
    if proc.returncode:
        return None
    try:
        node = json.loads(out).get("Node") or {}
    except ValueError:
        return None
    return node.get("ComputedName") or (node.get("Name") or "").split(".")[0] or None


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _text(v: Any, n: int = 80) -> str | None:
    return str(v)[:n] if v not in (None, "") else None


class McpClients:
    def __init__(self, whois: Callable[[str], Awaitable[str | None]] = tailscale_name):
        self._clients: dict[tuple, dict] = {}
        self._names: dict[str, str | None] = {}
        self._whois = whois

    def _touch(self, key: tuple, **fields: Any) -> dict:
        now = time.time()
        c = self._clients.get(key)
        if c is None:
            c = self._clients[key] = {"client": None, "version": None, "first_seen": now, "requests": 0,
                                      "tool_calls": 0, "last_tool": None, "connects": 0}
        c.update({k: v for k, v in fields.items() if v is not None})
        c["last_seen"] = now
        c["requests"] += 1
        self._prune(now)
        return c

    def _prune(self, now: float) -> None:
        for k in [k for k, c in self._clients.items() if now - c["last_seen"] > KEEP_S]:
            del self._clients[k]
        while len(self._clients) > MAX_CLIENTS:
            del self._clients[min(self._clients, key=lambda k: self._clients[k]["last_seen"])]

    def note_http(self, ip: str | None, user_agent: str | None, user: str | None, body: bytes) -> None:
        """One request to /mcp. `body` is the JSON-RPC message (or batch) it carried, if any."""
        try:
            ip = str(ipaddress.ip_address((ip or "").strip()))
        except ValueError:
            ip = None
        c = self._touch(("http", ip, _text(user_agent, 120)), transport="http", ip=ip,
                        user_agent=_text(user_agent, 120), user=_text(user))
        try:
            data = json.loads(body) if body else []
        except ValueError:
            return
        for m in data if isinstance(data, list) else [data]:
            if not isinstance(m, dict):
                continue
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            if m.get("method") == "initialize":
                info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
                c["client"], c["version"] = _text(info.get("name")), _text(info.get("version"), 40)
                c["connects"] += 1
            elif m.get("method") == "tools/call":
                c["tool_calls"] += 1
                c["last_tool"] = _text(params.get("name"))

    def note_stdio(self, header: str) -> None:
        """A hub request from the stdio server, with its X-MCP-Client header ({pid, name, version, tool?})."""
        try:
            info = json.loads(header[:1000])
        except ValueError:
            return
        if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
            return
        c = self._touch(("stdio", info["pid"]), transport="stdio", pid=info["pid"],
                        client=_text(info.get("name")), version=_text(info.get("version"), 40))
        if info.get("tool"):
            c["tool_calls"] += 1
            c["last_tool"] = _text(info["tool"])
        else:
            c["connects"] += 1

    async def report(self) -> dict:
        now = time.time()
        self._prune(now)
        todo = {c["ip"] for c in self._clients.values() if c.get("ip") and c["ip"] not in self._names}
        for ip in todo:
            if ipaddress.ip_address(ip).is_loopback:
                self._names[ip] = "this Mac"
        todo = [ip for ip in todo if ip not in self._names]
        if todo:
            names = await asyncio.gather(*(self._whois(ip) for ip in todo), return_exceptions=True)
            self._names.update({ip: n if isinstance(n, str) else None for ip, n in zip(todo, names)})
        out = []
        for c in sorted(self._clients.values(), key=lambda c: c["last_seen"], reverse=True):
            stdio = c["transport"] == "stdio"
            out.append({
                "transport": c["transport"], "client": c["client"], "version": c["version"],
                "device": "this Mac" if stdio else self._names.get(c.get("ip") or ""),
                "ip": c.get("ip"), "user": c.get("user"), "user_agent": c.get("user_agent"), "pid": c.get("pid"),
                "connected": _alive(c.get("pid")) if stdio else now - c["last_seen"] < ACTIVE_S,
                "first_seen": c["first_seen"], "last_seen": c["last_seen"], "requests": c["requests"],
                "connects": c["connects"], "tool_calls": c["tool_calls"], "last_tool": c["last_tool"],
            })
        return {"clients": out, "connected": sum(c["connected"] for c in out), "active_s": ACTIVE_S}
