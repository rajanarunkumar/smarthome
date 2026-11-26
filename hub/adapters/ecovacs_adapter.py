"""Ecovacs Deebot robot vacuums through Ecovacs' cloud (deebot-client, the
library Home Assistant uses). Sign in with the Ecovacs app's email and password
in Settings; the hub keeps only a hash of the password, never the password.

deebot-client needs Python 3.13+. Without it the hub runs with Ecovacs off.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)

try:
    import aiohttp
    from deebot_client.api_client import ApiClient
    from deebot_client.authentication import Authenticator, create_rest_config
    from deebot_client.commands.json.clean import CleanAction
    from deebot_client.device import Device as Bot
    from deebot_client.events import BatteryEvent, StateEvent
    from deebot_client.exceptions import DeebotError
    from deebot_client.models import State
    from deebot_client.mqtt_client import MqttClient, create_mqtt_config
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    class DeebotError(Exception):
        pass

STATE_LABELS = {"IDLE": "Idle", "CLEANING": "Cleaning", "RETURNING": "Returning to dock",
                "DOCKED": "Docked", "ERROR": "Needs attention", "PAUSED": "Paused"}
ACTIONS = {"clean": "Clean", "pause": "Pause", "resume": "Resume", "dock": "Return to dock"}


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def actions_for(state: str | None) -> list[dict[str, str]]:
    """Buttons that make sense in the robot's current state."""
    ids = {"CLEANING": ["pause", "dock"], "PAUSED": ["resume", "dock"], "RETURNING": ["clean"],
           "ERROR": ["dock"]}.get(state or "", ["clean"])
    return [{"id": i, "label": ACTIONS[i]} for i in ids]


class EcovacsAdapter(Adapter):
    brand = "ecovacs"
    label = "Ecovacs"
    prefixes = ("ecovacs",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._session: Any = None
        self._mqtt: Any = None
        self._bots: dict[str, Any] = {}      # hub id -> deebot Device
        self._state: dict[str, dict] = {}   # hub id -> {"state", "battery", "seen"}
        self._starting = asyncio.Lock()

    @property
    def config(self) -> dict:
        return self.secrets.get("ecovacs") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("password_hash"))

    async def start(self) -> None:
        if self.active:
            asyncio.create_task(self._connect_quietly())

    async def _connect_quietly(self) -> None:
        try:
            await self._connect(self.config)
        except Exception as exc:  # noqa: BLE001 - the hub keeps running without Ecovacs
            log.warning("Ecovacs did not connect: %s", exc)

    async def _connect(self, cfg: dict) -> int:
        async with self._starting:
            await self.close()
            self._session = aiohttp.ClientSession()
            rest = create_rest_config(self._session, device_id=cfg["device_id"], alpha_2_country=cfg["country"])
            auth = Authenticator(rest, cfg["account"], cfg["password_hash"])
            found = await ApiClient(auth).get_devices()
            self._mqtt = MqttClient(create_mqtt_config(device_id=cfg["device_id"], country=cfg["country"]), auth)
            for info in found.mqtt:
                bot = Bot(info, auth)
                hid = "ecovacs:" + str(bot.device_info.get("did") or bot.device_info.get("name")).lower()
                self._bots[hid] = bot
                self._state[hid] = {}
                self._subscribe(hid, bot)
                await bot.initialize(self._mqtt)
            return len(found.mqtt)

    def _subscribe(self, hid: str, bot: Any) -> None:
        async def on_state(event: Any) -> None:
            self._state[hid].update(state=event.state.name, seen=time.monotonic())

        async def on_battery(event: Any) -> None:
            self._state[hid].update(battery=event.value, seen=time.monotonic())

        bot.events.subscribe(StateEvent, on_state)
        bot.events.subscribe(BatteryEvent, on_battery)

    async def connect(self, account: str, password: str, country: str) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Ecovacs support needs Python 3.13 or newer on the hub Mac. See the README.")
        cfg = {"account": account.strip(), "password_hash": md5(password), "country": country.upper(),
               "device_id": self.config.get("device_id") or md5(str(time.time()))}
        try:
            count = await self._connect(cfg)
        except DeebotError as exc:
            await self.close()
            raise HubError(401, f"Ecovacs sign-in failed: {exc}") from exc
        self.secrets.set("ecovacs", cfg)
        return {"connected": True, "devices": count}

    async def disconnect(self) -> None:
        await self.close()
        self.secrets.set("ecovacs", None)

    async def devices(self) -> list[Device]:
        out = []
        for hid, bot in self._bots.items():
            st = self._state.get(hid, {})
            state = st.get("state")
            info = bot.device_info
            out.append(Device(
                id=hid, brand=self.brand, kind="vacuum",
                name=info.get("nick") or info.get("deviceName") or "Deebot",
                model=info.get("deviceName"), online=bool(st), on=state == "CLEANING",
                media=STATE_LABELS.get(state or "", "Connecting…"), battery=st.get("battery"),
                actions=actions_for(state), capabilities=["onoff", "actions"],
            ))
        return out

    def names(self) -> dict[str, str]:
        return {h: b.device_info.get("nick") or "Deebot" for h, b in self._bots.items()}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        bot = self._bots.get(device_id)
        if bot is None:
            raise HubError(404, f"Unknown Ecovacs robot {device_id}")
        current = self._state.get(device_id, {}).get("state")
        # The row switch: on = clean (or resume), off = return to the dock.
        action = state.action or {True: "resume" if current == "PAUSED" else "clean", False: "dock"}.get(state.on)
        if action not in ACTIONS:
            raise HubError(400, "Unknown robot command")
        caps = bot.capabilities
        command = (caps.charge.execute() if action == "dock" else caps.clean.action.command(
            {"clean": CleanAction.START, "pause": CleanAction.PAUSE, "resume": CleanAction.RESUME}[action]))
        await bot.execute_command(command)

    async def status(self) -> dict:
        return {"connected": bool(self._bots) or self.active, "available": AVAILABLE,
                "country": self.config.get("country"), "devices": len(self._bots)}

    async def close(self) -> None:
        for bot in self._bots.values():
            try:
                await bot.teardown()
            except Exception:  # noqa: BLE001
                pass
        self._bots.clear()
        if self._mqtt is not None:
            await self._mqtt.disconnect()
            self._mqtt = None
        if self._session is not None:
            await self._session.close()
            self._session = None
