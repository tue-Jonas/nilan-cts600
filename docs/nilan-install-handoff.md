# Nilan CTS600 install handoff — user steps + agent/Paperclip delegation

This is the practical install guide for TUE-16/TUE-20 after the ESP32 bridge was flashed.

## Current state

- ESP board: Waveshare ESP32-S3-RS485-CAN.
- Firmware: ESPHome raw serial-to-TCP bridge, not Modbus-TCP.
- ESP address: `192.168.1.139` / `nilan-bridge.local`.
- ESP web UI: `http://192.168.1.139/`.
- Raw bridge TCP: `192.168.1.139:6638`.
- Server brain: current live stack is on TJ-LT at `~/nilan-cts600`.
- Server safety: `NILAN_READ_ONLY=1` is enabled, so REST writes return `403` and MQTT command topics are ignored during first live bringup.
- Current live stack is still mock mode: `NILAN_MOCKUP=1`. Switch to real mode only after RS485 wiring is done.

## 2026-06-19 — Live bringup diagnosis (Wattson)

On-site wiring done by Jonas; test driven remotely from TJ-LT (same WLAN).

- **Verified working:** ESP online at `192.168.1.139:6638`; the CTS600 **answers** on the
  bus (consistent 31-byte reply to `REPORT_SLAVE_ID`, for node 3 and 30). Wiring/power/
  panel-replacement all correct.
- **Bug found:** firmware UART was `parity: EVEN, stop_bits: 1` (**8E1**). The CTS600 +
  frodef driver use **8N2** (`parity='N', stopbits=2`). With 8E1 every Modbus CRC on the
  reply fails (bytes mangled by EVEN-parity handling) → driver aborts on
  `unknown function code 0x44`.
- **Fix applied** in `firmware/nilan-bridge.yaml`: `parity: NONE`, `stop_bits: 2`.
  **Needs one reflash** (OTA from TJ-LT once `firmware/secrets.yaml` is present, or USB on-site).
- **Still to fix after a good read:** `/etc/nilan/socat.env` has `ESP_BRIDGE_HOST` placeholder
  **and** port `502` (must be `192.168.1.139:6638`). Confirm node address (env says 30,
  frodef default is 3).

## Reference links

- Paperclip parent issue: `https://tj-lt.tail34a5cf.ts.net/TUE/issues/TUE-16`
- Paperclip implementation issue: `https://tj-lt.tail34a5cf.ts.net/TUE/issues/TUE-20`
- Paperclip server issue: `https://tj-lt.tail34a5cf.ts.net/TUE/issues/TUE-22`
- ESP web UI: `http://192.168.1.139/`
- Waveshare board docs: `https://www.waveshare.net/wiki/ESP32-S3-RS485-CAN`
- ESPHome stream server component: `https://github.com/oxan/esphome-stream-server`
- frodef CTS600 driver/integration: `https://github.com/frodef/nilan-cts600-homeassistant`

## Safety boundaries

Do not treat this as mains electrical work. The RS485/control-panel connector is low-voltage, but the Nilan unit contains 230 V internally.

Hard rules:

1. Power off the Nilan unit before opening anything.
2. Do not touch mains wiring or power supply sections.
3. Do not connect `12V` or `GND` to RS485 data terminals.
4. Do not connect the original CTS600 panel and the ESP as two masters at the same time.
5. First live test is read-only: no fan/mode/temp writes until real status polling is stable.

## What Jonas physically does

### Step 1 — Prepare

Bring:

- The flashed ESP32-S3-RS485-CAN board.
- USB-C power cable/power supply for the ESP.
- Two short wires for RS485 data.
- Small screwdriver.
- Phone camera.

Before touching wires:

1. Open `http://192.168.1.139/` on the same network and confirm the ESP UI loads.
2. Take a photo of the Nilan panel/connector before disconnecting anything.
3. Send/post the photo to TUE-16 if anything differs from `12V / A / /B / GND`.

### Step 2 — Power down

1. Turn off power to the Nilan unit.
2. Wait until the panel/display is off.
3. Only then open the panel/control area.

### Step 3 — Disconnect original panel

1. Find the green CTS600/control-panel terminal block.
2. Expected labels: `12V`, `A`, `/B`, `GND`.
3. Disconnect the existing CTS600 panel/control cable from that block.
4. Keep the original panel cable available as rollback.

Rollback is simple: power off, remove ESP RS485 wires, reconnect original panel as before.

### Step 4 — Wire RS485 data only

With Nilan still powered off:

1. Connect Nilan `A` to Waveshare RS485 `A+`.
2. Connect Nilan `/B` to Waveshare RS485 `B-`.
3. Do not connect Nilan `12V` yet.
4. Do not connect Nilan `GND` yet unless an agent explicitly asks after diagnosing a signal issue.
5. Keep the ESP powered over USB-C for first live read-only test.

If reads fail later, possible fixes are swapping `A`/`B`, adding shared `GND`, or checking termination. Do not try these randomly; let an agent guide it from logs.

### Step 5 — Power up for read-only test

1. Power the ESP from USB-C.
2. Power the Nilan unit back on.
3. Tell the agent: `wired and powered`.
4. Do not click any fan/mode/temp controls.

## What to hand off to agents/Paperclip

### Agent task A — Switch backend to real read-only mode

Ask an agent:

```text
TUE-16 Nilan is wired and powered. On TJ-LT, switch ~/nilan-cts600 from NILAN_MOCKUP=1 to NILAN_MOCKUP=0, keep NILAN_READ_ONLY=1, keep ESP_IP=192.168.1.139 and ESP_PORT=6638. Recreate nilan-api, watch logs, and verify only read-only /api/status + MQTT state. Do not send write commands.
```

Expected agent commands on TJ-LT:

```bash
cd ~/nilan-cts600
sed -i 's/^NILAN_MOCKUP=.*/NILAN_MOCKUP=0/' env/nilan.env
grep -E '^(NILAN_MOCKUP|NILAN_READ_ONLY|ESP_IP|ESP_PORT)=' env/nilan.env
docker compose up -d nilan-api
docker compose logs -f nilan-api
```

Acceptance criteria:

- `nilan-api` remains healthy.
- Socat connects to `192.168.1.139:6638`.
- `/api/status` returns `mockup:false`.
- `connected:true` after a successful poll.
- `last_error:null` or no recurring protocol error.
- MQTT retained `nilan/state` updates.

### Agent task B — Diagnose if no real reads appear

Use this prompt if `/api/status` is not connected:

```text
TUE-16 real read-only bringup failed. Diagnose without writes. Check nilan-api logs, socat connection, ESP bridge reachability, and likely RS485 A/B polarity. Do not disable NILAN_READ_ONLY and do not send fan/mode/temp commands.
```

Likely outcomes:

- TCP to ESP fails: Wi-Fi/IP/power problem.
- TCP connects but no CTS600 response: RS485 wiring/polarity/panel still connected/wrong connector.
- Intermittent/protocol errors: try swapping `A` and `/B`, then retest.
- Still no data: consider shared `GND` or termination, guided by logs.

### Agent task C — Enable controlled writes only after stable reads

Only after real status is stable for several minutes:

```text
TUE-16 read-only status is stable. Prepare a controlled write test plan but do not execute without confirmation. Keep changes minimal: fan 1->2->1, then setpoint readback. Explain rollback.
```

Before any write:

- Confirm Nilan is behaving normally.
- Confirm the original panel is disconnected.
- Confirm `/api/status` has sane temperatures and fan/mode state.
- Confirm Jonas is present and can hear/observe the unit.

Then an agent may temporarily set `NILAN_READ_ONLY=0` and run exactly one small test, with user confirmation.

## What to post back to Paperclip

After each phase, post a TUE-16 comment with:

- Physical wiring state.
- Whether original panel is disconnected.
- ESP IP and TCP status.
- Backend mode (`NILAN_MOCKUP`, `NILAN_READ_ONLY`).
- `/api/status` result summary.
- MQTT result summary.
- Any errors and exact next action.

## Current known endpoints

From TJ-PC:

```bash
curl -I http://192.168.1.139/
nc -vz 192.168.1.139 6638
```

From TJ-LT:

```bash
nc -vz 192.168.1.139 6638
cd ~/nilan-cts600
docker compose ps
docker exec nilan-api python - <<'PY'
import urllib.request
for path in ("/healthz", "/api/status"):
    with urllib.request.urlopen("http://127.0.0.1:8642" + path, timeout=5) as r:
        print(path, r.status, r.read().decode()[:1000])
PY
```

## Stop conditions

Stop and ask an agent before continuing if:

- Connector labels are not exactly understood.
- There are more than four wires or a different panel connector than expected.
- The ESP web UI disappears after moving it.
- Nilan behaves unexpectedly after power-up.
- Logs show repeated protocol errors after swapping A/B once.
- You are unsure whether a terminal is data or power.
