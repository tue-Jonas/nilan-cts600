# Nilan CTS600 Architecture, Setup, Safety Gates, and Debugging

This document is the restart point for humans and agents working on the Nilan
CTS600 stack. It describes the current live deployment, the repository shape,
the safety gates, and the first debugging path without relying on issue-thread
history.

## Current Deployment

| Area | Current owner/host | Notes |
|---|---|---|
| Firmware build/USB flashing | `TJ-PC` | Use this host for local ESPHome work when the ESP must be connected by USB. |
| Live daemon/API/MQTT stack | `TJ-LT` | Current running stack lives at `~/nilan-cts600`. |
| ESP bridge | `192.168.1.139` / `nilan-bridge.local` | Waveshare ESP32-S3-RS485-CAN on the home WLAN. |
| ESP raw serial stream | TCP `192.168.1.139:6638` | Raw byte stream from ESPHome `stream_server`; not Modbus-TCP. |
| ESPHome API | TCP `192.168.1.139:6053` | Used by ESPHome/OTA tooling, with secrets in local ESPHome files only. |
| ESP web UI | `http://192.168.1.139/` | Basic ESPHome device diagnostics. |
| Nilan web/API proxy | `http://tj-lt:8643/` | Caddy basic-auth proxy to the `nilan-api` container. |
| Public dashboard slot | `https://tj-lt.tail34a5cf.ts.net:8443/` | Tailscale Funnel to local Caddy. Keep writes locked unless exposure is reviewed. |

Do not store live passwords, ESPHome OTA keys, Wi-Fi credentials, MQTT
passwords, Caddy plaintext passwords, or API bearer tokens in git. The local
secret-bearing files are:

- `env/nilan.env`: runtime mode, MQTT password, optional API token, ESP target.
- `mosquitto/passwd`: hashed MQTT users for Mosquitto.
- `caddy/Caddyfile`: Caddy basic-auth hash only; never add plaintext.
- `firmware/secrets.yaml`: ESPHome Wi-Fi/API/OTA/AP secrets, if firmware is
  built from this checkout.

## Repository Layout

| Path | Purpose |
|---|---|
| `firmware/nilan-bridge.yaml` | ESPHome firmware for the raw WLAN-to-RS485 bridge. |
| `app/nilan_api.py` | FastAPI REST API, dashboard serving, MQTT bridge, frodef CTS600 wrapper. |
| `app/entrypoint.sh` | Container startup; starts `socat` in real mode and then `uvicorn`. |
| `app/dashboard.html` | Self-hosted Nilan dashboard served by `GET /`. |
| `vendor/nilan_cts600.py` | Pinned frodef CTS600 protocol implementation. |
| `docker-compose.yml` | Live stack definition: `mosquitto`, `nilan-api`, `caddy`. |
| `env/nilan.env.example` | Safe template for local runtime config. |
| `mosquitto/mosquitto.conf` | Authenticated local Mosquitto config. |
| `caddy/Caddyfile` | Reverse proxy and basic-auth hash for web/API access. |
| `systemd/socat-nilan.service` | Optional host-side `socat`; not required for the compose deployment. |
| `docs/nilan-install-handoff.md` | Physical install and bring-up handoff. |

## Architecture

The ESP is deliberately simple:

```text
Nilan CTS600 RS485 bus
  -> ESP32-S3-RS485-CAN raw serial-to-TCP bridge on TCP 6638
  -> TJ-LT nilan-api container
  -> socat creates /dev/ttyNILAN
  -> frodef CTS600 driver speaks the CTS600 protocol
  -> FastAPI REST, dashboard, and MQTT state/command topics
```

The ESP must not translate protocol semantics. It is not a Modbus-TCP gateway.
The server side owns protocol parsing, polling, command serialization, retries,
state projection, REST validation, and MQTT integration.

Serial framing is `19200 8N2` (`data_bits: 8`, `parity: NONE`,
`stop_bits: 2`). The earlier `8E1` assumption was proven wrong during live
bring-up: replies arrived, but bytes were mangled and CRC validation failed.

## Runtime Modes and Safety Gates

`NILAN_MOCKUP` controls whether the daemon talks to real hardware.

| Value | Behavior |
|---|---|
| `NILAN_MOCKUP=1` | Uses frodef `CTS600Mockup`; no ESP, RS485 bus, or `socat` needed. |
| `NILAN_MOCKUP=0` | Starts `socat` from `ESP_IP:ESP_PORT` to `/dev/ttyNILAN` and uses the real unit. |

`NILAN_READ_ONLY` controls writes.

| Value | REST behavior | MQTT behavior |
|---|---|---|
| `NILAN_READ_ONLY=1` | `POST /api/fan`, `/api/mode`, and `/api/temp` return HTTP `403`. | `nilan/*/set` command messages are logged and ignored. |
| `NILAN_READ_ONLY=0` | Validated writes are sent to the CTS600 through the serialized device lock. | Command topics are accepted and executed through the same write path. |

First physical bring-up must keep `NILAN_READ_ONLY=1` until real status polling
is stable and the unit behavior has been observed locally.

## First Physical Bring-Up

Physical safety rules:

1. Power off the Nilan unit before opening the panel/control area.
2. Disconnect the original CTS600 panel before using the ESP. Do not run two
   masters on the panel bus.
3. Wire only data for the first test: Nilan `A` to Waveshare `A+`, Nilan `/B`
   to Waveshare `B-`.
4. Do not connect Nilan `12V`.
5. Do not connect Nilan `GND` unless debugging later points to a signal
   reference problem.
6. Power the ESP by USB-C for the first test.
7. Keep `NILAN_READ_ONLY=1` and verify real status reads before any write.

Rollback is physical and simple: power off the Nilan unit, remove the ESP RS485
wires, reconnect the original CTS600 panel cable exactly as before, then power
the Nilan unit back on.

Server-side first real read-only switch on `TJ-LT`:

```bash
cd ~/nilan-cts600
sed -i 's/^NILAN_MOCKUP=.*/NILAN_MOCKUP=0/' env/nilan.env
sed -i 's/^NILAN_READ_ONLY=.*/NILAN_READ_ONLY=1/' env/nilan.env
sed -i 's/^ESP_IP=.*/ESP_IP=192.168.1.139/' env/nilan.env
sed -i 's/^ESP_PORT=.*/ESP_PORT=6638/' env/nilan.env
docker compose up -d nilan-api
docker compose logs -f nilan-api
```

Expected before writes are considered:

- `nilan-api` stays healthy.
- Logs show the `socat` tunnel to `192.168.1.139:6638`.
- `GET /api/status` returns `mockup:false` and `read_only:true`.
- `connected:true` appears after a successful poll.
- `last_error` is null or does not repeat.
- Retained MQTT `nilan/state` updates.

## Compose and Systemd Operations

Run live stack commands on `TJ-LT` in `~/nilan-cts600`.

Start or restart the stack:

```bash
docker compose up -d
```

Rebuild the API image after editing Python, dashboard, requirements, or Docker
inputs:

```bash
docker compose up -d --build nilan-api
```

View service state and health:

```bash
docker compose ps
docker inspect --format '{{json .State.Health}}' nilan-api | jq
```

Read logs:

```bash
docker compose logs --tail=200 nilan-api
docker compose logs --tail=200 mosquitto
docker compose logs --tail=200 caddy
docker compose logs -f nilan-api
```

Health/API checks from `TJ-LT`:

```bash
curl -fsS http://127.0.0.1:8642/healthz | jq
curl -fsS http://127.0.0.1:8642/api/status | jq
curl -fsS http://127.0.0.1:8642/api/meta | jq
```

Authenticated proxy check:

```bash
curl -fsS -u nilan:'<password-from-local-secret-store>' http://127.0.0.1:8643/api/status | jq
```

Rollback to safe software state:

```bash
cd ~/nilan-cts600
sed -i 's/^NILAN_READ_ONLY=.*/NILAN_READ_ONLY=1/' env/nilan.env
docker compose up -d nilan-api
```

Rollback to mockup:

```bash
cd ~/nilan-cts600
sed -i 's/^NILAN_MOCKUP=.*/NILAN_MOCKUP=1/' env/nilan.env
sed -i 's/^NILAN_READ_ONLY=.*/NILAN_READ_ONLY=1/' env/nilan.env
docker compose up -d nilan-api
```

Optional host-side `systemd/socat-nilan.service` is only for non-container
consumers that need `/dev/ttyNILAN` on the host. The compose stack already runs
its own supervised `socat` loop inside `nilan-api`.

Install or inspect optional host-side unit:

```bash
sudo cp systemd/socat-nilan.service /etc/systemd/system/socat-nilan.service
sudo systemctl daemon-reload
sudo systemctl enable --now socat-nilan
sudo systemctl status socat-nilan
journalctl -u socat-nilan -n 100 -f
```

Rollback optional host-side unit:

```bash
sudo systemctl disable --now socat-nilan
sudo rm -f /etc/systemd/system/socat-nilan.service
sudo systemctl daemon-reload
```

## API Surface

`nilan-api` serves REST on internal port `8642`; Caddy exposes it on `8643`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Web dashboard. |
| `GET` | `/healthz` | Container health and basic flags. |
| `GET` | `/api/meta` | Self-describing labels, units, config envelope, controls, bridge target. |
| `GET` | `/api/status` | Current projected state plus raw decoded snapshot. |
| `GET` | `/api/activity?include_reads=false&limit=100` | Bounded in-memory activity log; read polls are summarized and hidden unless requested. |
| `POST` | `/api/fan` | Body `{"level":0..4}`; `0` means off. |
| `POST` | `/api/mode` | Body `{"mode":"auto"|"heat"|"cool"|"off"}`. |
| `POST` | `/api/temp` | Body `{"setpoint":5..30}`. |

`/api/status` includes at least:

- `t_room`, `t_supply`, `t_exhaust`
- `fan_level`, `mode`, `setpoint`, `status`, `display`
- `sensors`: decoded `T*` sensor values
- `raw`: full frodef snapshot
- `connected`, `mockup`, `read_only`, `last_update_ts`, `last_error`

Writes use the same device lock as the poller and MQTT commands. A protocol
failure should return `502` instead of crashing the service.

## MQTT Surface

The compose broker is internal on `mosquitto:1883` and bound to
`127.0.0.1:1884` for local debugging. Authentication is required.

Default base topic is `nilan`.

| Direction | Topic | Payload |
|---|---|---|
| publish retained | `nilan/state` | JSON with the same shape as `/api/status`. |
| publish retained LWT | `nilan/availability` | `online` while connected to broker; `offline` on MQTT last will. |
| subscribe command | `nilan/fan/set` | Raw `0`..`4` or JSON `{"level":2}`. |
| subscribe command | `nilan/mode/set` | Raw mode or JSON `{"mode":"auto"}`. |
| subscribe command | `nilan/temp/set` | Raw setpoint or JSON `{"setpoint":21}`. |

Debug retained messages:

```bash
mosquitto_sub -h 127.0.0.1 -p 1884 -u nilan -P '<mqtt-password>' -t 'nilan/#' -C 2 -v
mosquitto_pub -h 127.0.0.1 -p 1884 -u nilan -P '<mqtt-password>' -t 'nilan/fan/set' -m '2'
```

Do not publish command topics when `NILAN_READ_ONLY=0` unless a controlled write
test is approved and a local observer can verify the unit.

## Debugging Flows

### ESP Web UI Unreachable

Symptoms: `http://192.168.1.139/` does not load; ESPHome device appears offline.

Check:

```bash
ping -c 3 192.168.1.139
curl -I --max-time 5 http://192.168.1.139/
```

Likely causes:

- ESP unpowered or wrong USB power source.
- Wi-Fi dropped or DHCP/static IP changed.
- ESP booted into setup AP because WLAN credentials failed.

Next actions:

- Confirm ESP power and LEDs locally.
- Check router/DHCP lease for `nilan-bridge`.
- Reflash or update Wi-Fi secrets from `TJ-PC` if the device is no longer on the WLAN.

### TCP 6638 Closed

Symptoms: ESP web UI works, but `nc -vz 192.168.1.139 6638` fails or times out.

Check:

```bash
nc -vz 192.168.1.139 6638
docker compose logs --tail=100 nilan-api
```

Likely causes:

- ESP firmware without `stream_server`.
- Wrong firmware build or failed OTA.
- `ESP_IP` or `ESP_PORT` wrong in `env/nilan.env`.

Next actions:

- Confirm firmware includes `stream_server` on port `6638`.
- Verify `ESP_IP=192.168.1.139` and `ESP_PORT=6638`.
- Reflash the ESP from `TJ-PC` if the firmware does not expose the stream.

### TCP Connects but No CTS600 Frames

Symptoms: `socat` connects, but `/api/status` remains disconnected or logs show
timeouts/no response.

Check:

```bash
docker compose logs --tail=200 nilan-api
curl -fsS http://127.0.0.1:8642/api/status | jq '{connected,last_error,mockup,read_only}'
```

Likely causes:

- Original CTS600 panel is still connected and contending as bus master.
- Wrong connector or no RS485 connection.
- A/B polarity reversed.
- Nilan unit is powered off.

Next actions:

- Power off before touching wiring.
- Confirm original panel is disconnected.
- Confirm Nilan `A` goes to Waveshare `A+` and `/B` goes to `B-`.
- If logs still show no response, swap `A` and `/B` once, then retest.
- If still failing, consider shared `GND` or termination only with local guidance.

### Protocol/CRC Errors or Unknown Function Codes

Symptoms: bytes arrive, but logs show CRC/protocol errors or unexpected function
codes.

Likely causes:

- Wrong UART framing.
- Bad wiring/noise.
- Wrong CTS600 node address or protocol assumption.

Required firmware framing:

```yaml
baud_rate: 19200
data_bits: 8
parity: NONE
stop_bits: 2
```

Next actions:

- Confirm the ESP is running the current `firmware/nilan-bridge.yaml`.
- Reflash if an older `8E1` firmware may still be installed.
- Keep `NILAN_READ_ONLY=1`; do not test writes during protocol instability.

### REST Writes Return 403

This is expected when `NILAN_READ_ONLY=1`.

Check:

```bash
grep '^NILAN_READ_ONLY=' env/nilan.env
curl -i -X POST http://127.0.0.1:8642/api/fan \
  -H 'Content-Type: application/json' \
  -d '{"level":2}'
```

Next actions:

- Leave it read-only during bring-up and debugging.
- Only set `NILAN_READ_ONLY=0` for a controlled write test after stable real
  status is proven and an explicit approval path exists.

### MQTT Commands Ignored

This is expected when `NILAN_READ_ONLY=1`; logs should say the MQTT command was
ignored in read-only mode.

Check:

```bash
docker compose logs --tail=100 nilan-api | grep -i 'read-only\\|mqtt'
mosquitto_sub -h 127.0.0.1 -p 1884 -u nilan -P '<mqtt-password>' -t 'nilan/#' -v
```

Next actions:

- If read-only is intended, no fix is needed.
- If a controlled write test is approved, unlock read-only through
  `env/nilan.env`, restart `nilan-api`, run the single approved command, then
  restore read-only.

### MQTT Unavailable

Symptoms: no `nilan/state`, broker auth failures, or `nilan-api` cannot connect
to broker.

Check:

```bash
docker compose ps mosquitto
docker compose logs --tail=200 mosquitto
docker compose logs --tail=200 nilan-api | grep -i mqtt
mosquitto_sub -h 127.0.0.1 -p 1884 -u nilan -P '<mqtt-password>' -t 'nilan/availability' -C 1 -v
```

Likely causes:

- Missing or unreadable `mosquitto/passwd`.
- Wrong `MQTT_USER`/`MQTT_PASS` in `env/nilan.env`.
- Broker container unhealthy or not started.

Next actions:

- Recreate `mosquitto/passwd` with `mosquitto_passwd` and keep it readable by
  the in-container user.
- Confirm `MQTT_HOST` is injected as `mosquitto` by compose.
- Restart `mosquitto`, then `nilan-api`.

### Unhealthy `nilan-api` Container

Symptoms: `docker compose ps` shows unhealthy, dashboard/API unavailable, or
healthcheck fails.

Check:

```bash
docker compose ps nilan-api
docker inspect --format '{{json .State.Health}}' nilan-api | jq
docker compose logs --tail=200 nilan-api
```

Likely causes:

- Python startup/import error.
- `NILAN_MOCKUP=0` with missing `ESP_IP`.
- `socat`/device path problem during real mode startup.
- Port or dependency problem after rebuild.

Next actions:

- If the live hardware path is not needed, roll back to `NILAN_MOCKUP=1` and
  `NILAN_READ_ONLY=1`.
- If real mode is needed, fix `ESP_IP`, confirm TCP `6638`, then restart only
  `nilan-api`.

## Activity Log Ownership

Activity-log work belongs inside this repo next to the API because `nilan-api`
is the source of truth for validated commands, polling results, errors, and
MQTT command handling. The current implementation is a bounded in-memory log in
`app/nilan_api.py`, exposed through `GET /api/activity` and surfaced in
`app/dashboard.html`.

Current API behavior:

- `GET /api/activity?limit=100&include_reads=false` returns recent command and
  write-path events.
- `include_reads=true` also includes summarized status-poll events.
- `NILAN_ACTIVITY_LOG_MAX` bounds retained events.
- `NILAN_ACTIVITY_READ_SUMMARY_SECONDS` and
  `NILAN_ACTIVITY_READ_SUMMARY_COUNT` control read-poll summary flushing.
- Log entries redact fields whose keys look like passwords, tokens, secrets, or
  keys.

Current event fields include:

- `id`
- `ts` and `timestamp`
- `actor` / `source`: e.g. `rest-api`, `mqtt`, `device-poller`
- `action_type`: e.g. `status_poll`, `set_fan`, `set_mode`, `set_temp`,
  `command`
- `target`: REST path or MQTT subtopic
- `result`: `ok`, `blocked`, or `error`
- `safety_state`: `read_only` or `write_enabled`
- `detail`, `value`, and `count` when applicable

If durable history is required later, add it as an explicit follow-up:

- API/event capture may stay in `app/nilan_api.py` or move to a small helper
  such as `app/activity_log.py`.
- Durable local storage should be a mounted non-git path such as
  `data/activity-log.jsonl`.
- HomeBoard integration, if needed later, should consume the API/log output
  read-only rather than duplicate write state.

The activity log must not store passwords, bearer tokens, basic-auth headers,
ESPHome secrets, Wi-Fi secrets, MQTT passwords, or raw private request headers.
