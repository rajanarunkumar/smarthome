"""Nest thermostats through Google's Smart Device Management (SDM) API, the official way in.

Set up once (see README "Nest thermostat"):
1. Register for Device Access at console.nest.google.com/device-access (Google charges a one-time US$5).
2. In Google Cloud: enable the Smart Device Management API and create an OAuth client (Web application)
   whose authorized redirect URI is the hub's address with a trailing slash, the same one Google
   sign-in uses. Create a Device Access project with that client ID.
3. Settings → Nest: paste the Device Access project ID, client ID and client secret, then
   "Link Google account" and choose the thermostats to share.
4. Optional, for live events: enable Pub/Sub on the Device Access project, create a pull subscription
   on its topic, and paste the subscription path (projects/<gcp-project>/subscriptions/<name>) in
   Settings → Nest. The link then also asks for Pub/Sub access, so the hub pulls events with the same
   Google account: no service-account key to keep.

The hub keeps the refresh token and asks Google for short-lived access tokens. Readings refresh at most
once a minute (Google limits how often the API may be called). With events on, every trait change
(temperature, humidity, heating/cooling starting or stopping, mode, setpoints, Eco) is applied to the
cached thermostat as soon as Google publishes it, and handed to `on_update` so the HVAC recorder logs
run starts and stops at the minute they happen rather than at the next 2-minute sample.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets as pysecrets
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)

SDM_URL = "https://smartdevicemanagement.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://nestservices.google.com/partnerconnections/{project}/auth"
SCOPE = "https://www.googleapis.com/auth/sdm.service"
PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"
PUBSUB_URL = "https://pubsub.googleapis.com/v1"
EVENTS_KEPT = 200
LINK_SECONDS = 900
REFRESH_SECONDS = 60
T = "sdm.devices.traits."
C = "sdm.devices.commands."
MODES = ("HEAT", "COOL", "HEATCOOL", "OFF")
MIN_GAP_F = 3        # Nest keeps heat and cool setpoints at least ~1.5 °C apart


def c_to_f(c: float | None) -> int | None:
    return None if c is None else round(c * 9 / 5 + 32)


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 2)


def describe(dev: dict) -> dict:
    """The thermostat's state from its SDM traits, in °F."""
    traits = dev.get("traits", {})
    t = lambda name: traits.get(T + name, {})  # noqa: E731
    setpoint = t("ThermostatTemperatureSetpoint")
    eco = t("ThermostatEco")
    eco_on = eco.get("mode") == "MANUAL_ECO"
    fan = t("Fan")
    return {
        "current": c_to_f(t("Temperature").get("ambientTemperatureCelsius")),
        "humidity": t("Humidity").get("ambientHumidityPercent"),
        "mode": t("ThermostatMode").get("mode", "OFF"),
        "modes": [m for m in t("ThermostatMode").get("availableModes", []) if m in MODES],
        "hvac": t("ThermostatHvac").get("status", "OFF"),
        "heat": c_to_f(eco.get("heatCelsius") if eco_on else setpoint.get("heatCelsius")),
        "cool": c_to_f(eco.get("coolCelsius") if eco_on else setpoint.get("coolCelsius")),
        "eco": eco_on if eco else None,
        "fan": fan.get("timerMode") if fan else None,
        "online": t("Connectivity").get("status", "ONLINE") == "ONLINE",
        "unit": t("Settings").get("temperatureScale", "FAHRENHEIT"),
    }


def summary(s: dict) -> str:
    action = {"HEATING": "Heating", "COOLING": "Cooling"}.get(s["hvac"])
    if s["mode"] == "OFF":
        head = "Off"
    elif s["eco"]:
        head = "Eco"
    elif s["mode"] == "HEATCOOL":
        head = f"{action or 'Holding'} {s['heat']}–{s['cool']}°"
    else:
        target = s["heat"] if s["mode"] == "HEAT" else s["cool"]
        head = f"{action} to {target}°" if action else f"{s['mode'].title()} {target}°"
    return f"{head} · {s['current']}° inside" if s["current"] is not None else head


def device_name(dev: dict) -> str:
    custom = dev.get("traits", {}).get(T + "Info", {}).get("customName")
    if custom:
        return custom
    rooms = [p.get("displayName") for p in dev.get("parentRelations", []) if p.get("displayName")]
    return f"{rooms[0]} Thermostat" if rooms else "Nest Thermostat"


class NestAdapter(Adapter):
    brand = "nest"
    label = "Nest"
    prefixes = ("nest",)
    can_rename = False

    def __init__(self, secrets: Secrets, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.secrets = secrets
        self._http = httpx.AsyncClient(timeout=20, transport=transport)
        self._access: tuple[str, float] | None = None
        self._devices: list[dict] = []
        self._devices_at = 0.0
        self._pending: dict[str, tuple[str, float]] = {}   # state -> (origin, expires)
        self._last_mode: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.error: str | None = None
        self._events_task: asyncio.Task | None = None
        self.events: deque[dict] = deque(maxlen=EVENTS_KEPT)
        self.events_error: str | None = None
        self.last_event_at: str | None = None
        # Called with (hub device id, name, describe() state) whenever an event changes a thermostat.
        self.on_update: Callable[[str, str, dict], None] | None = None

    @property
    def config(self) -> dict:
        return self.secrets.get("nest") or {}

    @property
    def active(self) -> bool:
        return bool(self.config.get("refresh_token"))

    # ---- linking ----
    def configure(self, project_id: str, client_id: str, client_secret: str, subscription: str | None = None) -> None:
        cfg = {"project_id": project_id.strip(), "client_id": client_id.strip(), "client_secret": client_secret.strip()}
        if subscription and subscription.strip():
            cfg["subscription"] = subscription.strip()
        self.secrets.set("nest", cfg)
        self._access, self._devices, self._devices_at = None, [], 0.0

    @property
    def subscription(self) -> str | None:
        return self.config.get("subscription") or None

    def _scopes(self) -> str:
        return SCOPE + (" " + PUBSUB_SCOPE if self.subscription else "")

    def link_url(self, origin: str) -> str:
        cfg = self.config
        if not (cfg.get("project_id") and cfg.get("client_id") and cfg.get("client_secret")):
            raise HubError(409, "Save the Device Access project ID, client ID and client secret first.")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[1] > now}
        state = "nest." + pysecrets.token_urlsafe(16)
        self._pending[state] = (origin, now + LINK_SECONDS)
        query = urlencode({"redirect_uri": origin + "/", "access_type": "offline", "prompt": "consent",
                           "client_id": cfg["client_id"], "response_type": "code", "scope": self._scopes(), "state": state})
        return AUTH_URL.format(project=cfg["project_id"]) + "?" + query

    async def finish_link(self, origin: str, code: str, state: str) -> dict:
        pending = self._pending.pop(state, None)
        if pending is None or pending[1] < time.time():
            raise HubError(400, "That Nest link expired or was already used. Tap Link Google account again.")
        if pending[0] != origin:
            raise HubError(400, "The Nest link started on a different address.")
        cfg = self.config
        body = await self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": origin + "/",
                                          "client_id": cfg["client_id"], "client_secret": cfg["client_secret"]})
        if not body.get("refresh_token"):
            raise HubError(400, "Google didn't send a refresh token. Remove the hub's access at myaccount.google.com → "
                                "Security → Third-party access, then link again.")
        self.secrets.set("nest", {**cfg, "refresh_token": body["refresh_token"]})
        self._access = (body["access_token"], time.monotonic() + body.get("expires_in", 3600) - 60)
        await self._list(force=True)   # Google starts publishing events only after this first call
        self._start_events()
        return {"connected": True, "devices": len(self._thermostats())}

    async def disconnect(self) -> None:
        await self._stop_events()
        token = self.config.get("refresh_token")
        if token:
            try:
                await self._http.post("https://oauth2.googleapis.com/revoke", data={"token": token})
            except httpx.HTTPError:
                pass
        self.secrets.set("nest", None)
        self._access, self._devices, self._devices_at = None, [], 0.0

    # ---- Google calls ----
    async def _token_request(self, data: dict) -> dict:
        try:
            resp = await self._http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise HubError(504, f"Couldn't reach Google: {exc}") from exc
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200:
            err = body.get("error", resp.status_code)
            if err == "invalid_grant":
                raise HubError(401, "Google no longer accepts the hub's Nest link (it was removed, or the OAuth app is in "
                                    "Testing mode, where links expire after 7 days). Link the Google account again.")
            if err == "invalid_client":
                raise HubError(401, "Google rejected the client ID or client secret. Check them in Settings → Nest.")
            if err == "redirect_uri_mismatch":
                raise HubError(400, "Add this hub's address (with a trailing /) as an authorized redirect URI on the OAuth client.")
            raise HubError(502, f"Google said {err}: {body.get('error_description', '')}".strip())
        return body

    async def _token(self) -> str:
        if self._access and self._access[1] > time.monotonic():
            return self._access[0]
        cfg = self.config
        try:
            body = await self._token_request({"grant_type": "refresh_token", "refresh_token": cfg.get("refresh_token", ""),
                                              "client_id": cfg.get("client_id", ""), "client_secret": cfg.get("client_secret", "")})
        except HubError as exc:
            self.error = exc.message
            raise
        self._access = (body["access_token"], time.monotonic() + body.get("expires_in", 3600) - 60)
        return self._access[0]

    async def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        token = await self._token()
        try:
            resp = await self._http.request(method, f"{SDM_URL}/{path}", json=json, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise HubError(504, f"Couldn't reach Google: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 401:
            self._access = None
        if resp.status_code == 429:
            raise HubError(429, "Google limits how often a thermostat can be changed. Wait a minute and try again.")
        if resp.status_code >= 400:
            message = (body.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            if resp.status_code == 403 or "not been used" in message or "disabled" in message:
                message += " Check that the Smart Device Management API is enabled in the Google Cloud project."
            raise HubError(502 if resp.status_code >= 500 else 400, f"Nest: {message}")
        return body

    async def _list(self, force: bool = False) -> list[dict]:
        async with self._lock:
            if force or time.monotonic() - self._devices_at > REFRESH_SECONDS:
                body = await self._call("GET", f"enterprises/{self.config['project_id']}/devices")
                self._devices = body.get("devices", [])
                self._devices_at = time.monotonic()
                self.error = None
        return self._devices

    def _thermostats(self) -> list[dict]:
        return [d for d in self._devices if d.get("type") == "sdm.devices.types.THERMOSTAT"]

    @staticmethod
    def _hub_id(dev: dict) -> str:
        return "nest:" + dev["name"].rsplit("/", 1)[-1]

    def _find(self, device_id: str) -> dict:
        for dev in self._thermostats():
            if self._hub_id(dev) == device_id:
                return dev
        raise HubError(404, "Unknown Nest thermostat")


    # ---- events (Pub/Sub pull, with the linked account's own token) ----
    async def start(self) -> None:
        self._start_events()

    def _start_events(self) -> None:
        if self.active and self.subscription and (self._events_task is None or self._events_task.done()):
            self._events_task = asyncio.create_task(self._events_loop())

    async def _stop_events(self) -> None:
        task, self._events_task = self._events_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _events_loop(self) -> None:
        delay = 15
        while self.active and self.subscription:
            try:
                got = await self.pull_events()
                self.events_error, delay = None, 15
                if not got:
                    await asyncio.sleep(2)   # pull already waited for messages; brief pause between empty pulls
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep pulling; say why in Settings
                self.events_error = exc.message if isinstance(exc, HubError) else str(exc)
                log.warning("Nest events: %s", self.events_error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 600)

    async def _pubsub(self, path: str, body: dict, timeout: float = 20) -> dict:
        token = await self._token()
        try:
            resp = await self._http.post(f"{PUBSUB_URL}/{path}", json=body, timeout=timeout,
                                         headers={"Authorization": f"Bearer {token}"})
        except httpx.TimeoutException:
            return {}
        except httpx.HTTPError as exc:
            raise HubError(504, f"Couldn't reach Google Pub/Sub: {exc}") from exc
        if resp.status_code == 401:
            self._access = None
        if resp.status_code >= 400:
            try:
                message = (resp.json().get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            except ValueError:
                message = f"HTTP {resp.status_code}"
            if resp.status_code in (401, 403):
                message += (" Tap Link again so Google also grants Pub/Sub access, and check the account has the "
                            "Pub/Sub Subscriber role on the subscription.")
            elif resp.status_code == 404:
                message += " Check the subscription path in Settings → Nest."
            raise HubError(502 if resp.status_code >= 500 else 400, f"Nest events: {message}")
        return resp.json() if resp.content else {}

    async def pull_events(self) -> int:
        """One pull: apply each message, then acknowledge them. Returns how many arrived."""
        sub = self.subscription
        body = await self._pubsub(f"{sub}:pull", {"maxMessages": 100}, timeout=60)
        received = body.get("receivedMessages", [])
        for item in received:
            try:
                payload = json.loads(base64.b64decode(item.get("message", {}).get("data", "") or b"{}"))
            except ValueError:
                payload = {}
            self.apply_event(payload)
        ack = [r["ackId"] for r in received if r.get("ackId")]
        if ack:
            await self._pubsub(f"{sub}:acknowledge", {"ackIds": ack})
        return len(received)

    def apply_event(self, payload: dict) -> dict | None:
        """Merge an SDM event's traits into the cached device and remember the event."""
        update = payload.get("resourceUpdate") or {}
        name, traits = update.get("name"), update.get("traits") or {}
        stamp = payload.get("timestamp") or datetime.now().astimezone().isoformat(timespec="seconds")
        self.last_event_at = stamp
        dev = next((d for d in self._devices if d.get("name") == name), None)
        if dev is None:
            # A device the hub hasn't listed yet (or a relation change): reread the list next time.
            if name or payload.get("relationUpdate"):
                self._devices_at = 0.0
            return None
        for trait, value in traits.items():
            dev.setdefault("traits", {}).setdefault(trait, {}).update(value)
        if dev.get("type") != "sdm.devices.types.THERMOSTAT":
            return None
        device_id, state = self._hub_id(dev), describe(dev)
        event = {"t": stamp, "device_id": device_id, "name": device_name(dev),
                 "changed": sorted(t.removeprefix(T) for t in traits), "state": state, "summary": summary(state)}
        self.events.appendleft(event)
        if self.on_update:
            try:
                self.on_update(device_id, event["name"], state)
            except Exception as exc:  # noqa: BLE001 - a recorder problem must not stop events
                log.warning("Nest event recorder: %s", exc)
        return event

    def recent_events(self, limit: int = 50) -> list[dict]:
        return list(self.events)[:limit]

    # ---- devices ----
    async def devices(self) -> list[Device]:
        await self._list()
        out = []
        for dev in self._thermostats():
            s = describe(dev)
            if s["mode"] != "OFF":
                self._last_mode[self._hub_id(dev)] = s["mode"]
            details = []   # humidity and fan are shown with the thermostat controls
            out.append(Device(id=self._hub_id(dev), brand=self.brand, kind="thermostat", name=device_name(dev),
                              model="Nest Thermostat", online=s["online"], on=s["hvac"] in ("HEATING", "COOLING"),
                              media=summary(s), details=details, thermostat=s, capabilities=["thermostat"]))
        return out

    def names(self) -> dict[str, str]:
        return {self._hub_id(d): device_name(d) for d in self._thermostats()}

    async def _command(self, dev: dict, command: str, params: dict) -> None:
        await self._call("POST", f"{dev['name']}:executeCommand", {"command": C + command, "params": params})

    async def set_state(self, device_id: str, state: StateIn) -> None:
        await self._list()
        dev = self._find(device_id)
        s = describe(dev)
        mode = state.thermostat_mode
        if state.on is False:
            mode = "OFF"
        elif state.on is True and s["mode"] == "OFF":
            mode = self._last_mode.get(device_id) or next((m for m in ("HEAT", "COOL", "HEATCOOL") if m in s["modes"]), "HEAT")
        if mode and mode not in (s["modes"] or MODES):
            raise HubError(400, f"This thermostat doesn't offer {mode.title()} mode.")
        if state.eco is not None:
            await self._command(dev, "ThermostatEco.SetMode", {"mode": "MANUAL_ECO" if state.eco else "OFF"})
            s["eco"] = state.eco
        if mode and mode != s["mode"]:
            await self._command(dev, "ThermostatMode.SetMode", {"mode": mode})
            s["mode"] = mode
        if state.heat_f is not None or state.cool_f is not None:
            if s["eco"]:
                raise HubError(409, "Eco is on, so the temperature can't be set. Turn Eco off first.")
            if s["mode"] == "OFF":
                raise HubError(409, "The thermostat is off. Choose Heat, Cool or Heat·Cool first.")
            if s["mode"] == "HEATCOOL":
                heat = state.heat_f if state.heat_f is not None else s["heat"]
                cool = state.cool_f if state.cool_f is not None else s["cool"]
                if heat is None or cool is None or cool - heat < MIN_GAP_F:
                    raise HubError(400, f"Keep cooling at least {MIN_GAP_F}° above heating.")
                await self._command(dev, "ThermostatTemperatureSetpoint.SetRange", {"heatCelsius": f_to_c(heat), "coolCelsius": f_to_c(cool)})
            elif s["mode"] == "HEAT":
                if state.heat_f is None:
                    raise HubError(400, "In Heat mode, set the heating temperature.")
                await self._command(dev, "ThermostatTemperatureSetpoint.SetHeat", {"heatCelsius": f_to_c(state.heat_f)})
            else:
                if state.cool_f is None:
                    raise HubError(400, "In Cool mode, set the cooling temperature.")
                await self._command(dev, "ThermostatTemperatureSetpoint.SetCool", {"coolCelsius": f_to_c(state.cool_f)})
        if state.fan_minutes is not None:
            if state.fan_minutes:
                await self._command(dev, "Fan.SetTimer", {"timerMode": "ON", "duration": f"{state.fan_minutes * 60}s"})
            else:
                await self._command(dev, "Fan.SetTimer", {"timerMode": "OFF"})
        self._devices_at = 0.0   # read the result back next time

    async def status(self) -> dict:
        cfg = self.config
        return {"connected": bool(cfg.get("refresh_token")), "configured": bool(cfg.get("client_id")),
                "project_id": cfg.get("project_id"), "devices": len(self._thermostats()), "error": self.error,
                "subscription": self.subscription, "events_running": bool(self._events_task and not self._events_task.done()),
                "events_error": self.events_error, "last_event_at": self.last_event_at}

    async def close(self) -> None:
        await self._stop_events()
        await self._http.aclose()
