"""protect2mqtt: UniFi Protect air quality sensors (UP Air Quality), read from the console and published on MQTT.

Protect's official integration API lists an UP Air Quality but not its
readings. The console's own (private) API has them: each sensor in
``GET /proxy/protect/api/sensors`` carries an ``airQuality`` block with the
latest values and a status word for each. That API takes a logged-in session,
so this poller logs in as a local user (POST /api/auth/login, as the web app
does) and, every PROTECT_INTERVAL seconds, reads the sensor list once and
publishes, retained:

  protect/<sensor>/state         {"aqi", "co2_ppm", "pm2_5", "voc_index", "temp_c", "humidity", …, "status": {…}, "ts"}
  protect/<sensor>/info          {"name", "model", "firmware", "mac", "id"}
  protect/<sensor>/availability  online | offline

<sensor> is the sensor's name in Protect, lower case with underscores
("UP Air Quality" -> "up_air_quality").

It is read-only by construction: the only requests it can make are the login
and that one GET (anything else is refused before it is sent), and it
subscribes to nothing, so no MQTT message can make it act. Give it a local
Protect user with a view-only role.

With PROTECT_HA_DISCOVERY=1 it also publishes Home Assistant MQTT discovery for
the readings, so Home Assistant shows them with no integration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import aiomqtt
import httpx

log = logging.getLogger("protect2mqtt")

LOGIN = ("POST", "/api/auth/login")
SENSORS = ("GET", "/proxy/protect/api/sensors")
ALLOWED = frozenset({LOGIN, SENSORS})
MAX_BACKOFF = 300.0
INFO_EVERY = 3600.0

# The console's airQuality keys -> ours, with the unit each is in.
FIELDS = {
    "aqi": ("aqi", None), "co2": ("co2_ppm", "ppm"), "pm1p0": ("pm1", "µg/m³"), "pm2p5": ("pm2_5", "µg/m³"),
    "pm4p0": ("pm4", "µg/m³"), "pm10p0": ("pm10", "µg/m³"), "voc": ("voc_index", None), "nox": ("nox_index", None),
    "tvoc": ("tvoc", None), "temperature": ("temp_c", "°C"), "humidity": ("humidity", "%"), "vape": ("vape", None),
}


class Refused(ValueError):
    """A request that is not the login or the sensor read: never sent."""


class NotLoggedIn(Exception):
    """The console wants a (new) login."""


def check(method: str, path: str) -> None:
    if (method, path) not in ALLOWED:
        raise Refused(f"protect2mqtt only reads; {method} {path} is not sent")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "sensor"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def state_of(sensor: dict, now: float) -> dict | None:
    """The readings from one sensor's airQuality block; None for a sensor without one."""
    aq = sensor.get("airQuality")
    if not isinstance(aq, dict):
        return None
    out: dict[str, Any] = {"ts": now}
    status: dict[str, str] = {}
    for key, (field, _) in FIELDS.items():
        reading = aq.get(key)
        if isinstance(reading, dict):
            out[field] = _num(reading.get("value"))
            if isinstance(reading.get("status"), str):
                status[field] = reading["status"]
    out["status"] = status
    out["connected"] = sensor.get("state") == "CONNECTED"
    return out


def info_of(sensor: dict) -> dict:
    return {"name": sensor.get("name"), "model": sensor.get("type"), "firmware": sensor.get("firmwareVersion"),
            "mac": sensor.get("mac"), "id": sensor.get("id")}


# Home Assistant sensors: key in the state JSON -> (name, device class, icon).
HA_SENSORS = {
    "aqi": ("Air quality index", "aqi", None), "co2_ppm": ("CO₂", "carbon_dioxide", None),
    "pm1": ("PM1", "pm1", None), "pm2_5": ("PM2.5", "pm25", None), "pm4": ("PM4", None, "mdi:blur"),
    "pm10": ("PM10", "pm10", None), "voc_index": ("VOC index", None, "mdi:molecule"),
    "nox_index": ("NOx index", None, "mdi:molecule"), "tvoc": ("TVOC", None, "mdi:molecule"),
    "temp_c": ("Temperature", "temperature", None), "humidity": ("Humidity", "humidity", None),
    "vape": ("Vape", None, "mdi:smoke"),
}


def discovery(topic: str, info: dict, prefix: str = "homeassistant") -> list[tuple[str, dict]]:
    """(config topic, payload) for each reading, for Home Assistant's MQTT discovery."""
    mac = (info.get("mac") or "").lower() or slug(info.get("name") or "")
    device = {"identifiers": [f"protect2mqtt_{mac}"], "name": info.get("name") or "UniFi Protect sensor",
              "manufacturer": "Ubiquiti", "model": info.get("model"), "sw_version": info.get("firmware"),
              "connections": [["mac", info["mac"]]] if info.get("mac") else []}
    units = {field: unit for field, unit in FIELDS.values()}
    out = []
    for key, (name, dev_class, icon) in HA_SENSORS.items():
        config = {"name": name, "unique_id": f"protect2mqtt_{mac}_{key}", "state_topic": f"{topic}/state",
                  "value_template": f"{{{{ value_json.{key} }}}}", "availability_topic": f"{topic}/availability",
                  "state_class": "measurement", "device": device}
        for field, value in (("unit_of_measurement", units.get(key)), ("device_class", dev_class), ("icon", icon)):
            if value:
                config[field] = value
        out.append((f"{prefix}/sensor/protect2mqtt_{mac}/{key}/config", config))
    return out


class Console:
    """The Protect console: a login and the sensor list, nothing else."""

    def __init__(self, host: str, username: str, password: str, verify: bool = False, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = host if host.startswith("http") else f"https://{host}"
        self.username, self.password = username, password
        self.http = httpx.AsyncClient(base_url=self.base, verify=verify, timeout=timeout, transport=transport)

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        check(method, path)              # refuses anything but the two allowed requests, before sending
        return await self.http.request(method, path, **kw)

    async def login(self) -> None:
        r = await self._send(*LOGIN, json={"username": self.username, "password": self.password, "rememberMe": True})
        if r.status_code != 200:
            raise NotLoggedIn(f"login refused: HTTP {r.status_code}")

    async def sensors(self) -> list[dict]:
        r = await self._send(*SENSORS, headers={"accept": "application/json"})
        if r.status_code in (401, 403):
            raise NotLoggedIn(f"HTTP {r.status_code}")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"unexpected sensor list: {str(data)[:200]}")
        return data

    async def read(self) -> list[dict]:
        """The sensor list, logging in first or again when the console asks."""
        try:
            return await self.sensors()
        except NotLoggedIn:
            await self.login()
            return await self.sensors()

    async def close(self) -> None:
        await self.http.aclose()


async def run(console: Console, interval: float, mqtt: dict, base: str, ha_discovery: bool = False,
              ha_prefix: str = "homeassistant") -> None:
    will = aiomqtt.Will(f"{base}/protect2mqtt/availability", "offline", retain=True)
    async with aiomqtt.Client(hostname=mqtt["host"], port=mqtt["port"], username=mqtt["user"], password=mqtt["password"],
                              identifier="protect2mqtt", will=will) as client:
        await client.publish(f"{base}/protect2mqtt/availability", "online", retain=True)
        announced: dict[str, float] = {}      # topic -> when its info (and discovery) was last published
        online: dict[str, bool] = {}
        failures = 0
        while True:
            try:
                sensors = await console.read()
                now = time.time()
                for sensor in sensors:
                    state = state_of(sensor, now)
                    if state is None:
                        continue
                    topic = f"{base}/{slug(sensor.get('name') or sensor.get('id') or '')}"
                    if now - announced.get(topic, float("-inf")) > INFO_EVERY:
                        info = info_of(sensor)
                        await client.publish(f"{topic}/info", json.dumps(info), retain=True)
                        if ha_discovery:
                            for config_topic, config in discovery(topic, info, ha_prefix):
                                await client.publish(config_topic, json.dumps(config), retain=True)
                        if topic not in announced:
                            log.info("publishing %s (%s, firmware %s) every %.0f s", topic, info["model"], info["firmware"], interval)
                        announced[topic] = now
                    await client.publish(f"{topic}/state", json.dumps(state), retain=True)
                    if online.get(topic) is not state["connected"]:
                        await client.publish(f"{topic}/availability", "online" if state["connected"] else "offline", retain=True)
                        online[topic] = state["connected"]
                if failures:
                    log.info("the console is answering again")
                failures = 0
                await asyncio.sleep(interval)
            except (httpx.HTTPError, NotLoggedIn, ValueError) as exc:
                failures += 1
                if failures == 1:
                    log.warning("could not read the sensors (%s); retrying", exc)
                for topic in [t for t, up in online.items() if up]:
                    await client.publish(f"{topic}/availability", "offline", retain=True)
                    online[topic] = False
                # Back off harder on refused logins: the console rate-limits them.
                await asyncio.sleep(min(MAX_BACKOFF, interval * 2 ** min(failures, 6)))


async def start(interval: float, mqtt: dict, base: str, ha_discovery: bool, ha_prefix: str) -> None:
    console = Console(os.environ["PROTECT_HOST"].strip(), os.environ["PROTECT_USERNAME"], os.environ["PROTECT_PASSWORD"],
                      verify=os.environ.get("PROTECT_VERIFY_TLS", "0").strip().lower() in ("1", "true", "yes", "on"))
    try:
        await run(console, interval, mqtt, base, ha_discovery, ha_prefix)
    finally:
        await console.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # not a line per poll
    mqtt = {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", "1883")),
            "user": os.environ.get("MQTT_USERNAME") or None, "password": os.environ.get("MQTT_PASSWORD") or None}
    while True:
        try:
            asyncio.run(start(float(os.environ.get("PROTECT_INTERVAL", "15")), mqtt,
                            os.environ.get("PROTECT_TOPIC", "protect"),
                            os.environ.get("PROTECT_HA_DISCOVERY", "0").strip().lower() in ("1", "true", "yes", "on"),
                            os.environ.get("PROTECT_HA_PREFIX", "homeassistant")))
        except (OSError, aiomqtt.MqttError) as exc:
            log.warning("restarting in 30 s: %s", exc)
            time.sleep(30)


if __name__ == "__main__":
    main()
