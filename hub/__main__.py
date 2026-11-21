"""Run the hub:  python -m hub  [--port 8420]"""

from __future__ import annotations

import argparse
import logging
import socket

import uvicorn

from .config import Secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart home hub")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    parser.add_argument("--port", type=int, default=8420)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = Secrets().access_token()
    host = socket.gethostname().removesuffix(".local") + ".local"
    print("\nSmart home hub")
    print(f"  On this Mac:   http://localhost:{args.port}/#token={token}")
    print(f"  On your phone: http://{host}:{args.port}/#token={token}")
    print("  Open the link once; the app remembers the token.\n", flush=True)
    uvicorn.run("hub.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
