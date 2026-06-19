# Nilan CTS600 — Server Stack (TUE-22)

Server-side "brain" for the Nilan CTS600 WLAN control (parent: TUE-16). The
ESP32 is only a transparent **raw serial↔TCP tunnel**; the CTS600 custom
protocol is driven here by **frodef**'s `CTS600` class over a serial device,
and exposed as the REST + MQTT contract from TUE-16 plan §8.

```
Nilan ── RS485 ──> ESP32 (raw TCP :6638) ──WLAN──> [ socat ─> /dev/ttyNILAN ─> frodef ] ─> REST + MQTT
```

## Architecture decision

Chose **standalone frodef core + thin FastAPI/MQTT wrapper** (plan §7 sanctioned
alternative) over full Home Assistant:

- The §8 contract (`/api/*` + `nilan/...` topics) is custom; HA would still need
  a shim to remap its native API/MQTT-discovery topics to it. The wrapper
  implements §8 directly with less total glue.
- Far lighter on `tj-lt` (OOM-prone: paperclip + wameling + task-inbox already
  resident). Whole stack is capped well under 0.5 GB vs HA's ~1–2 GB.
- `frodef`'s protocol library (`vendor/nilan_cts600.py`, pinned commit in
  `vendor/FRODEF_COMMIT.txt`) imports cleanly standalone, incl. `CTS600Mockup`.

Reversible: if the board prefers full HA, frodef is its native HACS integration;
the socat/broker/proxy layers here are reused as-is.

## Layout

| Path | Purpose |
|---|---|
| `vendor/nilan_cts600.py` | Pinned frodef CTS600 protocol lib (incl. mockup) |
| `app/nilan_api.py` | FastAPI REST + MQTT wrapper (implements plan §8) |
| `app/Dockerfile`, `app/entrypoint.sh` | API image; entrypoint runs socat sidecar in real mode |
| `docker-compose.yml` | `mosquitto` + `nilan-api` + `caddy` (auth proxy) |
| `mosquitto/` | broker config + `passwd` (gitignored) |
| `caddy/Caddyfile` | reverse proxy + basic auth on `:8643` |
| `env/nilan.env(.example)` | config + secrets (`.env` gitignored) |
| `systemd/socat-nilan.service` | OPTIONAL host-side socat (only for a host consumer; container has its own) |

## Bring-up NOW (no hardware — mockup)

```bash
cd ~/nilan-cts600
cp env/nilan.env.example env/nilan.env      # NILAN_MOCKUP=1 by default
# secrets:
PW=$(openssl rand -hex 12)
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 mosquitto_passwd -b -c /m/passwd nilan "$PW"
# mosquitto_passwd writes the file 0600 root; the in-container mosquitto user must read it:
docker run --rm -v "$PWD/mosquitto:/m" --entrypoint sh eclipse-mosquitto:2 -c 'chmod 0644 /m/passwd'
sed -i "s/^MQTT_PASS=.*/MQTT_PASS=$PW/" env/nilan.env
HASH=$(docker run --rm caddy:2 caddy hash-password --plaintext "$PW")
# put $HASH into caddy/Caddyfile (basic_auth nilan <hash>)
docker compose up -d --build
curl -s localhost:8642/api/status | jq      # via api directly (or :8643 w/ basic auth via caddy)
```

## Go live (ESP bridge installed)

When Wattson reports the live/static ESP IP on TUE-16:

```bash
cd ~/nilan-cts600
sed -i 's/^NILAN_MOCKUP=.*/NILAN_MOCKUP=0/' env/nilan.env
sed -i 's/^ESP_IP=.*/ESP_IP=<bridge-ip>/'  env/nilan.env
docker compose up -d
docker compose logs -f nilan-api   # expect socat tunnel + frodef reads T15/display
curl -s localhost:8642/api/status | jq    # t_room (T15) plausible, fan_level/mode/setpoint present
```

Then run the §9 control verification (fan 1→2→3, mode, setpoint set+readback) and
confirm the provisional `NILAN_T_SUPPLY_KEY`/`NILAN_T_EXHAUST_KEY` mapping against
the unit's register dump (TUE-16 §11).

## Web dashboard (TUE-55 — Phase 1, read-only)

A self-hosted, mobile-friendly **web UI** served by the same `nilan-api` app, so
it lives behind the same Caddy basic-auth as the API (local-first, no extra
service or port).

| What | Where |
|---|---|
| **URL (LAN)** | `http://tj-lt:8643/` (or `http://192.168.1.103:8643/`) |
| **URL (public, Tailscale Funnel)** | `https://tj-lt.tail34a5cf.ts.net:8443/` |
| **Login** | user `nilan` / password delivered via the TUE-55 Paperclip thread (not stored in git, since the endpoint is public) |
| **Auto-start** | `docker compose` `restart: unless-stopped` + `tailscale funnel --bg` (persisted) |
| **Refresh** | auto-polls `/api/status` every `NILAN_POLL_SECONDS` (mobile-aware) |

What it shows (Phase 1):

- Live tiles: Raum-/Zuluft-/Ablufttemperatur, Modus, Lüfterstufe, Solltemperatur, Status.
- **Alle Sensoren** + **Rohdaten** (the full decoded snapshot — every value the
  frodef driver scraped; humidity/filter/hours appear here automatically once the
  live unit is on the bus, no code change needed).
- **Konfiguration / Einstellungen** ("how it's configured"): mockup/live, read-only
  flag, poll interval, ESP-bridge target, sensor mapping, MQTT base topic, current
  mode/setpoint/fan/status.
- Clear **"Keine Live-Daten / Bridge offline"** banner when the bus is down.
- **Steuerung** (controls) are rendered but **locked** while `NILAN_READ_ONLY=1`
  (Phase 2 unlocks them once the live bus from TUE-54 is verified).

Endpoints added: `GET /` (dashboard), `GET /api/meta` (self-describing
labels/units/config), and `raw` + `read_only` fields on `GET /api/status`.

### Update / operate the dashboard

```bash
cd ~/nilan-cts600
# after editing app/dashboard.html or app/nilan_api.py:
docker compose up -d --build nilan-api      # rebuild + restart, ~10s

# change the web password:
docker run --rm caddy:2 caddy hash-password --plaintext '<new-pass>'
# paste the hash into caddy/Caddyfile (basic_auth nilan <hash>), then:
docker compose restart caddy
```

### Public access — Tailscale Funnel (TUE-55)

The dashboard is published to the public internet on Funnel port **8443**
(`https://tj-lt.tail34a5cf.ts.net:8443/`), proxying to the local Caddy on
`127.0.0.1:8643`. Funnel port `443` is already used by another service, so `8443`
is the dashboard's slot. Manage it with:

```bash
tailscale funnel status                                   # show all funnel mappings
tailscale funnel --bg --https=8443 http://127.0.0.1:8643  # (re)enable dashboard funnel
tailscale funnel --https=8443 off                         # take it OFF the public internet
```

**Security:** Funnel exposes the endpoint to the *whole internet*; the Caddy
basic-auth credential is the only gate, so it must stay strong (it is NOT in git).
Because `/api/*` is same-origin, once **Phase 2** flips `NILAN_READ_ONLY=0` the
*write* endpoints become publicly reachable behind that one password — reconsider
exposure then (keep control tailnet-only via `tailscale serve`, or add a second
factor) before unlocking writes on the public Funnel.

> **Phase 2 (control)** stays disabled until `TUE-54` (GND fix → live bus) is done
> and write-tests pass; then set `NILAN_READ_ONLY=0` and the UI controls activate.

## Contract (plan §8)

| Method | Endpoint / topic | Body / payload |
|---|---|---|
| GET | `/api/status` | `{t_room,t_supply,t_exhaust,fan_level,mode,setpoint,...}` |
| POST | `/api/fan` | `{"level":0-4}` (0=off) |
| POST | `/api/mode` | `{"mode":"auto\|heat\|cool\|off"}` |
| POST | `/api/temp` | `{"setpoint":5-30}` |
| MQTT sub | `nilan/fan/set`,`nilan/mode/set`,`nilan/temp/set` | as above (raw value or JSON) |
| MQTT pub | `nilan/state` (retained) | JSON, same shape as `/api/status` |
| MQTT pub | `nilan/availability` (retained, LWT) | `online`/`offline` |
