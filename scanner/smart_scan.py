#!/usr/bin/env python3
"""Discover smart-home devices on your local network.

Run this on a laptop connected to the SAME Wi-Fi as your devices:

    pip install zeroconf        # optional, enables mDNS/Matter/HomeKit discovery
    python3 smart_scan.py

It uses four read-only discovery methods (nothing is changed on any device):
  * mDNS / Bonjour  - Matter, HomeKit, Hue, Chromecast/Google, Alexa, AirPlay, Sonos...
  * SSDP / UPnP     - TVs (LG webOS, Sony Bravia), Hue bridge, Roku, Wemo, routers...
  * Kasa (TP-Link)  - legacy local protocol on UDP 9999
  * Govee LAN API   - UDP 4001/4002 (turn on "LAN Control" per device in the Govee app)
"""
import json
import re
import socket
import sys
import time
import urllib.request
from collections import defaultdict

TIMEOUT = 4  # seconds per discovery method

MDNS_TYPES = {
    "_matter._tcp.local.": "Matter (commissioned)",
    "_matterc._udp.local.": "Matter (ready to pair)",
    "_hap._tcp.local.": "Apple HomeKit",
    "_hap._udp.local.": "Apple HomeKit (Thread)",
    "_hue._tcp.local.": "Philips Hue bridge",
    "_googlecast._tcp.local.": "Google Cast / Nest / Google TV",
    "_amzn-wplay._tcp.local.": "Amazon Echo / Fire TV",
    "_airplay._tcp.local.": "AirPlay (TV / speaker)",
    "_sonos._tcp.local.": "Sonos",
    "_spotify-connect._tcp.local.": "Spotify Connect speaker",
    "_nanoleafapi._tcp.local.": "Nanoleaf",
    "_shelly._tcp.local.": "Shelly",
    "_esphomelib._tcp.local.": "ESPHome",
    "_home-assistant._tcp.local.": "Home Assistant",
    "_meshcop._udp.local.": "Thread border router",
    "_miio._udp.local.": "Xiaomi",
    "_ipp._tcp.local.": "Printer",
}

found = defaultdict(lambda: {"names": set(), "kinds": set(), "details": set()})


def add(ip, kind, name=None, detail=None):
    d = found[ip]
    d["kinds"].add(kind)
    if name:
        d["names"].add(name.strip())
    if detail:
        d["details"].add(detail.strip())


def scan_mdns():
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        print("  (skipped: run 'pip install zeroconf' to enable mDNS/Matter/HomeKit discovery)")
        return
    zc = Zeroconf()

    class Listener:
        def add_service(self, zc_, type_, name):
            info = zc_.get_service_info(type_, name, timeout=2000)
            if not info:
                return
            props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore")
                     for k, v in (info.properties or {}).items()}
            detail = ", ".join(f"{k}={props[k]}" for k in ("md", "fn", "vendor", "model", "ty") if props.get(k))
            for ip in info.parsed_addresses():
                add(ip, MDNS_TYPES[type_], name.split("." + type_)[0], detail or None)

        update_service = remove_service = lambda *a: None

    browsers = [ServiceBrowser(zc, t, Listener()) for t in MDNS_TYPES]
    time.sleep(TIMEOUT + 2)
    zc.close()
    del browsers


def scan_ssdp():
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n').encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(TIMEOUT)
    s.sendto(msg, ("239.255.255.250", 1900))
    locations = {}
    end = time.time() + TIMEOUT
    while time.time() < end:
        try:
            data, (ip, _) = s.recvfrom(4096)
        except socket.timeout:
            break
        text = data.decode(errors="ignore")
        server = re.search(r"(?im)^server:\s*(.+)$", text)
        loc = re.search(r"(?im)^location:\s*(\S+)$", text)
        add(ip, "UPnP/SSDP", detail=server.group(1) if server else None)
        if loc:
            locations.setdefault(ip, loc.group(1))
    s.close()
    for ip, url in locations.items():
        try:
            xml = urllib.request.urlopen(url, timeout=2).read(65536).decode(errors="ignore")
        except Exception:
            continue
        get = lambda tag: (re.search(rf"<{tag}>([^<]*)</{tag}>", xml) or [None, None])[1]
        add(ip, "UPnP/SSDP", get("friendlyName"),
            " ".join(filter(None, [get("manufacturer"), get("modelName")])) or None)


def _kasa_xor(data, decrypt=False):
    key, out = 171, bytearray()
    for b in data:
        out.append(key ^ b)
        key = b if decrypt else key ^ b
    return bytes(out)


def scan_kasa():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(TIMEOUT)
    s.sendto(_kasa_xor(b'{"system":{"get_sysinfo":{}}}'), ("255.255.255.255", 9999))
    end = time.time() + TIMEOUT
    while time.time() < end:
        try:
            data, (ip, _) = s.recvfrom(4096)
        except socket.timeout:
            break
        try:
            info = json.loads(_kasa_xor(data, decrypt=True))["system"]["get_sysinfo"]
            add(ip, "TP-Link Kasa", info.get("alias"), info.get("model"))
        except Exception:
            add(ip, "TP-Link Kasa")
    s.close()


def scan_govee():
    r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        r.bind(("", 4002))
    except OSError:
        print("  (skipped: port 4002 busy)")
        return
    r.settimeout(TIMEOUT)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.sendto(b'{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}', ("239.255.255.250", 4001))
    end = time.time() + TIMEOUT
    while time.time() < end:
        try:
            data, (ip, _) = r.recvfrom(4096)
        except socket.timeout:
            break
        try:
            d = json.loads(data)["msg"]["data"]
            add(ip, "Govee (LAN)", d.get("device"), d.get("sku"))
        except Exception:
            add(ip, "Govee (LAN)")
    r.close()
    s.close()


def main():
    for label, fn in [("mDNS / Matter / HomeKit", scan_mdns), ("SSDP / UPnP", scan_ssdp),
                      ("TP-Link Kasa", scan_kasa), ("Govee LAN", scan_govee)]:
        print(f"Scanning {label}...")
        try:
            fn()
        except Exception as e:  # keep going if one method fails
            print(f"  ({label} failed: {e})")

    if not found:
        print("\nNo devices answered. Make sure this computer is on the same Wi-Fi as your devices.")
        return
    print(f"\nFound {len(found)} device(s):\n")
    for ip in sorted(found, key=lambda x: tuple(int(p) if p.isdigit() else 0 for p in x.split("."))):
        d = found[ip]
        print(f"{ip:<16} {' | '.join(sorted(d['names'])) or '(no name)'}")
        print(f"{'':<16} type:   {', '.join(sorted(d['kinds']))}")
        for det in sorted(d["details"]):
            print(f"{'':<16} info:   {det}")
        print()
    print("Tip: also check your router's 'connected devices' page; cloud-only devices "
          "(many Smart Life/Tuya plugs) may not answer any local discovery.")


if __name__ == "__main__":
    sys.exit(main())
