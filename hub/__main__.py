"""Run the hub:  python -m hub  [--port 8420]"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import Secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart home hub")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="address to listen on (default: this Mac only; phones connect through Tailscale)",
    )
    parser.add_argument("--port", type=int, default=8420)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    origins = (Secrets().get("auth") or {}).get("origins") or [f"http://localhost:{args.port}"]
    print("\nSmart home hub")
    for origin in origins:
        print(f"  {origin}")
    print("  Sign in with Google, then turn on Face ID.\n", flush=True)
    uvicorn.run("hub.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
