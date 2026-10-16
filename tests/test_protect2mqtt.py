import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest

SRC = Path(__file__).parent.parent / "protect2mqtt.py"
sys.path.insert(0, str(SRC.parent))
import protect2mqtt as p2m  # noqa: E402

SENSOR = {
    "id": "0123456789abcdef01234567", "name": "UP Air Quality", "type": "UP-AirQuality", "mac": "AABBCCDDEEFF",
    "firmwareVersion": "1.0.16", "state": "CONNECTED",
    "airQuality": {"aqi": {"value": 2, "status": "neutral"}, "vape": {"value": 0, "status": "safe"},
                   "tvoc": {"value": 5.8, "status": "neutral"}, "pm1p0": {"value": 0.4, "status": "neutral"},
                   "pm2p5": {"value": 0.5, "status": "neutral"}, "pm4p0": {"value": 0.5, "status": "neutral"},
                   "pm10p0": {"value": 0.6, "status": "neutral"}, "humidity": {"value": 49.86, "status": "neutral"},
                   "temperature": {"value": 25.09, "status": "neutral"}, "voc": {"value": 102, "status": "neutral"},
                   "nox": {"value": 1, "status": "neutral"}, "co2": {"value": 825, "status": "high"}},
}
DOOR = {"id": "x", "name": "Front door", "type": "UP-Sense", "state": "CONNECTED", "airQuality": None}


def test_only_the_login_and_the_sensor_read_can_be_sent():
    assert p2m.ALLOWED == {("POST", "/api/auth/login"), ("GET", "/proxy/protect/api/sensors")}
    with pytest.raises(p2m.Refused):
        p2m.check("PATCH", "/proxy/protect/api/sensors/abc")
    with pytest.raises(p2m.Refused):
        p2m.check("GET", "/proxy/protect/api/bootstrap")
    text = SRC.read_text()
    assert ".subscribe(" not in text           # nothing on MQTT can reach it
    assert not re.search(r'"(PATCH|PUT|DELETE)"', text)      # no writing verb anywhere in the poller


def test_readings_and_their_status_words():
    s = p2m.state_of(SENSOR, 1000.0)
    assert s["ts"] == 1000.0 and s["connected"] is True
    assert (s["aqi"], s["co2_ppm"], s["pm2_5"], s["pm10"], s["voc_index"], s["nox_index"]) == (2, 825, 0.5, 0.6, 102, 1)
    assert (s["temp_c"], s["humidity"], s["tvoc"], s["vape"]) == (25.09, 49.86, 5.8, 0)
    assert s["status"]["co2_ppm"] == "high" and s["status"]["vape"] == "safe"
    assert p2m.state_of(DOOR, 1000.0) is None
    assert p2m.slug("UP Air Quality") == "up_air_quality"
    assert p2m.info_of(SENSOR) == {"name": "UP Air Quality", "model": "UP-AirQuality", "firmware": "1.0.16",
                                   "mac": "AABBCCDDEEFF", "id": "0123456789abcdef01234567"}


def test_discovery_is_read_only_sensors():
    configs = dict(p2m.discovery("protect/up_air_quality", p2m.info_of(SENSOR)))
    co2 = configs["homeassistant/sensor/protect2mqtt_aabbccddeeff/co2_ppm/config"]
    assert co2["unit_of_measurement"] == "ppm" and co2["device_class"] == "carbon_dioxide"
    assert co2["value_template"] == "{{ value_json.co2_ppm }}"
    assert all("command_topic" not in c for c in configs.values())


def test_it_logs_in_when_the_console_asks_and_then_reads():
    calls = []
    logged_in = {"yes": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/auth/login":
            logged_in["yes"] = True
            return httpx.Response(200, json={}, headers={"set-cookie": "TOKEN=abc; Path=/"})
        if not logged_in["yes"]:
            return httpx.Response(401, json={"error": "Unauthorized"})
        assert request.headers.get("cookie") == "TOKEN=abc"
        return httpx.Response(200, json=[SENSOR, DOOR])

    async def go():
        console = p2m.Console("console.local", "viewer", "pw", transport=httpx.MockTransport(handler))
        try:
            first = await console.read()
            second = await console.read()
        finally:
            await console.close()
        return first, second

    first, second = asyncio.run(go())
    assert [s["name"] for s in first] == ["UP Air Quality", "Front door"] and second == first
    assert calls == [("GET", "/proxy/protect/api/sensors"), ("POST", "/api/auth/login"),
                     ("GET", "/proxy/protect/api/sensors"), ("GET", "/proxy/protect/api/sensors")]


def test_a_refused_login_is_reported_not_retried_in_a_loop():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401 if request.url.path != "/api/auth/login" else 403)

    async def go():
        console = p2m.Console("console.local", "viewer", "wrong", transport=httpx.MockTransport(handler))
        try:
            await console.read()
        finally:
            await console.close()

    with pytest.raises(p2m.NotLoggedIn):
        asyncio.run(go())
