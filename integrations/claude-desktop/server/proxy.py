"""Claude Desktop extension: connects Claude to the smart home hub's MCP endpoint (/mcp).

Claude Desktop talks MCP over stdin/stdout; the hub serves MCP over HTTPS on the tailnet. This relays each
JSON-RPC message to the hub with the API token and writes the answer back. Python's standard library only,
so it runs with the python3 that comes with macOS.

Settings come from the extension's install form (Claude Desktop keeps the token in the keychain):
  SMARTHOME_URL    the hub's MCP address, e.g. https://<your-mac>.<tailnet>.ts.net/mcp
  SMARTHOME_TOKEN  the hub's API token
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("SMARTHOME_URL", "").strip()
TOKEN = os.environ.get("SMARTHOME_TOKEN", "").strip()
TIMEOUT = 120


def error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def relay(message: dict, protocol: str | None) -> dict | list | None:
    """POST one JSON-RPC message (or batch) to the hub; None for notifications the hub accepts silently."""
    msg_id = message.get("id") if isinstance(message, dict) else None
    if not URL.startswith("https://") and not URL.startswith("http://127.0.0.1") and not URL.startswith("http://localhost"):
        return error(msg_id, -32000, "Set the hub address (https://…/mcp) in the extension's settings.") if msg_id is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {TOKEN}"}
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    request = urllib.request.Request(URL, data=json.dumps(message).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
            body = r.read().decode()
            if not body.strip():
                return None
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
                return json.loads(data[-1]) if data else None
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            text = "The hub didn't accept the API token. Check it in the extension's settings."
        elif exc.code in (403, 421):
            text = "The hub refused this address. Use the hub's own https://…ts.net/mcp address."
        else:
            text = f"The hub answered {exc.code}: {exc.read().decode(errors='replace')[:200]}"
        return error(msg_id, -32000, text) if msg_id is not None else None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return error(msg_id, -32000, f"Couldn't reach the hub ({reason}). Is Tailscale on and the Mac awake?") \
            if msg_id is not None else None


def main() -> None:
    protocol = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("method") == "initialize":
            protocol = (message.get("params") or {}).get("protocolVersion")
        reply = relay(message, protocol)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
