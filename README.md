# protect2mqtt

Reads **UniFi Protect air quality sensors** (the UP Air Quality) from your
UniFi console and publishes their readings to MQTT: air quality index, CO₂,
PM1/2.5/4/10, VOC and NOx indexes, TVOC, temperature, humidity and vape
detection. Optionally it publishes **Home Assistant MQTT discovery** too, so the
readings show up in Home Assistant with no custom integration.

Protect's official integration API lists the sensor but not its readings (as of
Protect 7.3), and Home Assistant's UniFi Protect integration does not show them
either. The console's own API, the one the Protect web app uses, has them, so
that is what this reads.

It is **read-only by construction**. The only requests it can make are the
login and one read of the sensor list; anything else is refused before it is
sent. It subscribes to nothing, so nothing published on MQTT can make it act.

## What it publishes

Every `PROTECT_INTERVAL` seconds (default 15, how often the sensor updates), retained, for each sensor that reports air quality:

| Topic | Payload |
|---|---|
| `protect/<sensor>/state` | JSON: `aqi`, `co2_ppm`, `pm1`, `pm2_5`, `pm4`, `pm10`, `voc_index`, `nox_index`, `tvoc`, `temp_c`, `humidity`, `vape`, `status` (Protect's word for each: `neutral`, `safe`, `high`, …), `connected`, `ts` |
| `protect/<sensor>/info` | JSON: `name`, `model`, `firmware`, `mac`, `id` |
| `protect/<sensor>/availability` | `online` / `offline` |
| `protect/protect2mqtt/availability` | `online` / `offline` (the poller itself; also its MQTT last will) |

`<sensor>` is the sensor's name in Protect in lower case with underscores:
"UP Air Quality" becomes `up_air_quality`. Rename it in Protect to change it.

Example state:

```json
{"aqi": 3, "co2_ppm": 831, "pm1": 0.6, "pm2_5": 0.69, "pm4": 0.8, "pm10": 0.8, "voc_index": 101,
 "nox_index": 1, "tvoc": 5.8, "temp_c": 25.11, "humidity": 49.95, "vape": 0,
 "status": {"co2_ppm": "neutral", "vape": "safe"}, "connected": true, "ts": 1792159733.6}
```

VOC and NOx are Sensirion indexes (100 is the sensor's recent average for VOC,
1 is clean air for NOx), not concentrations. Protect does not say what unit its
TVOC figure is in, so none is given.

## Running it

1. In your UniFi console, add a **local** user (Admins & Users › Create New,
   "Restrict to local access only") with a **View Only** role for Protect. It
   needs no other app.
2. Copy `docker-compose.example.yml` to `docker-compose.yml`, fill it in and run
   `docker compose up -d --build`.

| Variable | Default | |
|---|---|---|
| `PROTECT_HOST` | (required) | The console's address. |
| `PROTECT_USERNAME`, `PROTECT_PASSWORD` | (required) | The local view-only user. |
| `PROTECT_INTERVAL` | `15` | Seconds between reads. |
| `PROTECT_VERIFY_TLS` | `0` | `1` to check the console's certificate (it is self-signed unless you replaced it). |
| `PROTECT_TOPIC` | `protect` | The topic prefix. |
| `PROTECT_HA_DISCOVERY` | `0` | `1` to publish Home Assistant discovery. |
| `PROTECT_HA_PREFIX` | `homeassistant` | Home Assistant's discovery prefix. |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` | port `1883` | Your broker. |

The console rate-limits logins, so after a refused login it waits longer before
trying again (up to five minutes). It logs in once and keeps the session,
logging in again only when the console asks.

## Notes

- The console's own API is not a published interface: a Protect update can
  change it. The readings are taken from each sensor's `airQuality` block in
  `GET /proxy/protect/api/sensors`.
- Tested with an UP Air Quality on firmware 1.0.16 and Protect 7.3.70.

## Development

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

MIT licensed. Not affiliated with Ubiquiti.
