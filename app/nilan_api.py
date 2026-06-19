"""
Nilan CTS600 — thin REST + MQTT wrapper around frodef's CTS600 protocol library.

Server-side "brain" for TUE-22 / TUE-16. The ESP32 is only a transparent
RAW serial<->TCP tunnel; the CTS600 custom protocol is driven here by
frodef's `nilan_cts600.CTS600` over a serial device (`/dev/ttyNILAN`, created
by socat from the ESP TCP stream).

Implements the API/MQTT contract from TUE-16 plan section 8:

  GET  /api/status -> { t_room, t_supply, t_exhaust, fan_level, mode, ... }
  POST /api/fan    <- { "level": 0-4 }          (0 = off)
  POST /api/mode   <- { "mode": "auto|heat|cool|off" }
  POST /api/temp   <- { "setpoint": 5-30 }

  MQTT subscribe: nilan/fan/set, nilan/mode/set, nilan/temp/set
  MQTT publish  : nilan/state  (retained JSON, same shape as /api/status)
                  nilan/availability ("online"/"offline", retained LWT)

Modes:
  NILAN_MOCKUP=1  -> use frodef CTS600Mockup (no hardware needed; for bring-up
                     and contract verification before the ESP bridge is live).
  NILAN_MOCKUP=0  -> real device on NILAN_PORT (default /dev/ttyNILAN).

This wrapper is deliberately defensive: a CTS600 protocol error on one request
must never take the whole service down. Reads are served from the last good
poll snapshot; writes are serialized behind a single device lock.
"""
from __future__ import annotations

import json
import os
import threading
import time
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
import uvicorn

# frodef protocol library (vendored under ../vendor, added to sys.path by entrypoint)
from nilan_cts600 import CTS600, CTS600Mockup, NilanCTS600Exception  # type: ignore

logging.basicConfig(level=os.environ.get("NILAN_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("nilan-api")

# ---- config -----------------------------------------------------------------
MOCKUP = os.environ.get("NILAN_MOCKUP", "0").strip() not in ("0", "", "false", "False")
PORT_DEV = os.environ.get("NILAN_PORT", "/dev/ttyNILAN")
RETRIES = int(os.environ.get("NILAN_RETRIES", "3"))
POLL_SECONDS = float(os.environ.get("NILAN_POLL_SECONDS", "30"))
# TUE-100: how long the bus may be disconnected before /healthz reports 503.
# Must exceed a couple of poll cycles so a single missed read / brief reconnect
# blip does NOT flap the healthcheck, but be short enough that a real outage
# trips the autoheal restart inside the ~1-2 min acceptance window.
HEALTH_STALE_SECONDS = float(os.environ.get("NILAN_HEALTH_STALE_SECONDS", "90"))
START_TS = time.time()  # process boot reference for the "never connected" case
T15_FALLBACK = float(os.environ.get("NILAN_T15_FALLBACK", "21"))
API_TOKEN = os.environ.get("NILAN_API_TOKEN", "").strip()  # optional bearer for direct access
READ_ONLY = os.environ.get("NILAN_READ_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
HTTP_HOST = os.environ.get("NILAN_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("NILAN_HTTP_PORT", "8642"))
ESP_IP = os.environ.get("ESP_IP", "").strip()       # live ESP bridge IP (real mode)
ESP_PORT = os.environ.get("ESP_PORT", "6638").strip()
# Panel language to set at connect so frodef's English SHOW-DATA menu regexes
# match (temperatures). Empty string disables. Default ENGLISH.
SET_LANGUAGE = os.environ.get("NILAN_SET_LANGUAGE", "ENGLISH").strip()

APP_VERSION = "1.1"  # 1.1 adds read-only web dashboard (TUE-55 Phase 1)
DASHBOARD_HTML = (Path(__file__).resolve().parent / "dashboard.html")

MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()  # empty -> MQTT disabled
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "").strip()
MQTT_PASS = os.environ.get("MQTT_PASS", "").strip()
MQTT_BASE = os.environ.get("MQTT_BASE_TOPIC", "nilan").strip().rstrip("/")

# Sensor->contract mapping. PROVISIONAL: exact supply/exhaust sensor ids are
# confirmed against the real unit's nameplate (TUE-16 plan section 11). Override
# via env without code changes once the live register dump is available.
T_ROOM_KEY = os.environ.get("NILAN_T_ROOM_KEY", "T15")
T_SUPPLY_KEY = os.environ.get("NILAN_T_SUPPLY_KEY", "T1")
T_EXHAUST_KEY = os.environ.get("NILAN_T_EXHAUST_KEY", "T5")

MODE_TO_FRODEF = {"auto": "AUTO", "heat": "HEAT", "cool": "COOL"}


def _canon_mode(raw) -> Optional[str]:
    """Normalise the frodef mode token to canonical auto|heat|cool|off.

    The token is language-dependent (DE unit reports KÜHLEN/HEIZEN/AUTO, and the
    CTS600 charset mangles umlauts e.g. 'KÚHLEN'), so match on ascii substrings."""
    if not raw:
        return None
    t = str(raw).upper()
    if "AUTO" in t:
        return "auto"
    if "HEIZ" in t or "HEAT" in t:
        return "heat"
    if "K" in t and "HL" in t or "COOL" in t:  # KÜHLEN/KÚHLEN/COOL
        return "cool"
    if "AUS" in t or "OFF" in t:
        return "off"
    return str(raw).lower()


# ---- device manager ---------------------------------------------------------
class NilanDevice:
    """Owns the single CTS600 connection, a poll thread, and a device lock.

    All serial access is serialized through `self._lock` so REST writes, MQTT
    writes, and the background poller never interleave on the bus.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {}
        self._connected = False
        self._last_error: Optional[str] = None
        self._last_update_ts: float = 0.0
        self._cts: Optional[CTS600] = None
        self._stop = threading.Event()
        self._on_state_change = None  # callback(dict) -> None, set by MQTT layer

    def set_state_callback(self, cb) -> None:
        self._on_state_change = cb

    # -- lifecycle --
    def connect(self) -> None:
        with self._lock:
            cls = CTS600Mockup if MOCKUP else CTS600
            log.info("Connecting CTS600 (%s, port=%s)", cls.__name__, "mockup" if MOCKUP else PORT_DEV)
            self._cts = cls(port=None if MOCKUP else PORT_DEV)
            self._cts.connect()
            self._cts.initialize()
            # Force the panel language to English: frodef's SHOW-DATA menu scan
            # matches English labels (STATUS, Tnn …°C). On a German unit those
            # regexes never match, so temperatures come back null. The physical
            # CTS600 panel is removed (ESP is the only master), so the display
            # language only affects what our scraper reads — safe to set.
            if not MOCKUP and SET_LANGUAGE:
                try:
                    ok = self._cts.setLanguage(SET_LANGUAGE)
                    log.info("setLanguage(%s) -> %s", SET_LANGUAGE, ok)
                except Exception as e:  # noqa: BLE001
                    log.warning("setLanguage(%s) failed (non-fatal): %s", SET_LANGUAGE, e)
            if READ_ONLY:
                log.info("Skipping setT15 fallback because NILAN_READ_ONLY is enabled")
            else:
                try:
                    self._cts.setT15(T15_FALLBACK)
                except Exception as e:  # noqa: BLE001
                    log.warning("setT15 fallback failed (non-fatal): %s", e)
            self._connected = True
            self._last_error = None

    def start(self) -> None:
        t = threading.Thread(target=self._poll_loop, name="nilan-poll", daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        # initial connect with retry
        while not self._stop.is_set():
            try:
                self.connect()
                break
            except Exception as e:  # noqa: BLE001
                self._connected = False
                self._last_error = str(e)
                log.warning("Connect failed, retrying in 10s: %s", e)
                self._stop.wait(10)
        # poll
        while not self._stop.is_set():
            try:
                self.refresh()
            except OSError as e:
                self._connected = False
                self._last_error = str(e)
                log.warning("Poll I/O error (%s) -> reconnecting before next poll", e)
                try:
                    self.connect()
                except Exception as ce:  # noqa: BLE001
                    self._last_error = str(ce)
                    log.warning("Reconnect after poll I/O error failed: %s", ce)
            except Exception as e:  # noqa: BLE001
                self._connected = False
                self._last_error = str(e)
                log.warning("Poll failed: %s", e)
            self._stop.wait(POLL_SECONDS)

    # -- core device ops (all under lock) --
    def refresh(self) -> dict[str, Any]:
        with self._lock:
            if not self._cts:
                raise RuntimeError("device not connected")
            # frodef's updateData(updateShowData) refreshes self.data from the unit.
            try:
                self._cts.updateData(True)
            except TypeError:
                # signature is updateData(updateDisplayData) in some revisions
                self._cts.updateData(updateDisplayData=True)
            # mockup populates self.data asynchronously; give it a moment
            if MOCKUP and not getattr(self._cts, "data", None):
                time.sleep(2.2)
            data = dict(getattr(self._cts, "data", {}) or {})
            self._snapshot = data
            self._last_update_ts = time.time()
            self._connected = True
            self._last_error = None
        if self._on_state_change:
            try:
                self._on_state_change(self.status())
            except Exception as e:  # noqa: BLE001
                log.warning("state callback failed: %s", e)
        return data

    def _retry(self, method: str, *args):
        """Run a device op (by method name) with resilient retries.

        The method is resolved on ``self._cts`` *each attempt* so that a
        reconnect — which replaces ``self._cts`` — is picked up on the next try.

        Retries on:
          * ``NilanCTS600Exception`` / ``TimeoutError`` — transient protocol/bus
            timeouts (single garbled frame). Plain retry.
          * ``OSError`` (e.g. ``[Errno 5] Input/output error``) — the socat PTY
            blipped/was re-created, so the serial handle is stale. Reconnect
            first, then retry. This is the write-path failure Jonas hit.
        """
        last = None
        for attempt in range(1, RETRIES + 1):
            try:
                return getattr(self._cts, method)(*args)
            except (NilanCTS600Exception, TimeoutError) as e:
                last = e
                log.warning("device op %s attempt %d/%d failed: %s",
                            method, attempt, RETRIES, e)
                time.sleep(0.3)
            except OSError as e:
                last = e
                log.warning("device op %s attempt %d/%d I/O error (%s) -> reconnecting",
                            method, attempt, RETRIES, e)
                try:
                    self.connect()
                except Exception as ce:  # noqa: BLE001
                    log.warning("reconnect after I/O error failed: %s", ce)
                time.sleep(0.6)
        raise last if last else RuntimeError("device op failed")

    def set_fan(self, level: int) -> None:
        with self._lock:
            if level == 0:
                self._retry("key_off")
            else:
                # ensure unit is on, then set flow 1-4
                try:
                    self._retry("key_on")
                except Exception:  # noqa: BLE001
                    pass
                self._retry("setFlow", level)
        self.refresh()

    def set_mode(self, mode: str) -> None:
        with self._lock:
            if mode == "off":
                self._retry("key_off")
            else:
                try:
                    self._retry("key_on")
                except Exception:  # noqa: BLE001
                    pass
                self._retry("setMode", MODE_TO_FRODEF[mode])
        self.refresh()

    def set_temp(self, setpoint: int) -> None:
        with self._lock:
            self._retry("setThermostat", setpoint)
        self.refresh()

    # -- contract projection (plan section 8) --
    def status(self) -> dict[str, Any]:
        d = self._snapshot
        status_txt = str(d.get("status", "")).upper()
        # 'off' is inferred when the unit reports an OFF/standby status
        if status_txt in ("OFF", "STANDBY", "STOP", "AUS"):
            mode = "off"
        else:
            mode = _canon_mode(d.get("mode"))

        def num(key):
            v = d.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        return {
            "t_room": num(T_ROOM_KEY),
            "t_supply": num(T_SUPPLY_KEY),
            "t_exhaust": num(T_EXHAUST_KEY),
            "fan_level": d.get("flow"),
            "mode": mode,
            "setpoint": d.get("thermostat"),
            "status": d.get("status"),
            "display": d.get("display"),
            "sensors": {k: v for k, v in d.items() if isinstance(k, str) and k.startswith("T")},
            # full decoded snapshot so the dashboard can show *every* value the
            # frodef driver scraped (humidity/filter/hours/etc. appear here once
            # the live unit is on the bus — keys are model/menu dependent).
            "raw": {k: v for k, v in d.items()},
            "connected": self._connected,
            "mockup": MOCKUP,
            "read_only": READ_ONLY,
            "last_update_ts": self._last_update_ts,
            "last_error": self._last_error,
        }


device = NilanDevice()


# ---- MQTT layer -------------------------------------------------------------
class MqttLayer:
    def __init__(self, dev: NilanDevice) -> None:
        self.dev = dev
        self.client = None

    def start(self) -> None:
        if not MQTT_HOST:
            log.info("MQTT disabled (MQTT_HOST unset)")
            return
        import paho.mqtt.client as mqtt  # local import so REST works without paho

        c = mqtt.Client(client_id="nilan-api")
        if MQTT_USER:
            c.username_pw_set(MQTT_USER, MQTT_PASS)
        c.will_set(f"{MQTT_BASE}/availability", "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        self.client = c
        self.dev.set_state_callback(self.publish_state)
        c.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        c.loop_start()
        log.info("MQTT connecting to %s:%s base=%s", MQTT_HOST, MQTT_PORT, MQTT_BASE)

    def _on_connect(self, client, userdata, flags, rc, *a):
        log.info("MQTT connected rc=%s", rc)
        client.publish(f"{MQTT_BASE}/availability", "online", qos=1, retain=True)
        for sub in ("fan/set", "mode/set", "temp/set"):
            client.subscribe(f"{MQTT_BASE}/{sub}", qos=1)
        self.publish_state(self.dev.status())

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "ignore").strip()
        topic = msg.topic
        log.info("MQTT msg %s = %s", topic, payload)
        if READ_ONLY:
            log.warning("Ignoring MQTT command in read-only mode: %s", topic)
            return
        try:
            if topic.endswith("/fan/set"):
                self.dev.set_fan(_coerce_level(payload))
            elif topic.endswith("/mode/set"):
                self.dev.set_mode(_coerce_mode(payload))
            elif topic.endswith("/temp/set"):
                self.dev.set_temp(_coerce_setpoint(payload))
        except Exception as e:  # noqa: BLE001
            log.warning("MQTT command failed on %s: %s", topic, e)

    def publish_state(self, state: dict) -> None:
        if not self.client:
            return
        self.client.publish(f"{MQTT_BASE}/state", json.dumps(state), qos=1, retain=True)


mqtt_layer = MqttLayer(device)


def _coerce_level(payload: str) -> int:
    try:
        v = json.loads(payload)
        v = v.get("level") if isinstance(v, dict) else v
    except Exception:  # noqa: BLE001
        v = payload
    level = int(v)
    if not 0 <= level <= 4:
        raise ValueError("level out of range 0-4")
    return level


def _coerce_mode(payload: str) -> str:
    try:
        v = json.loads(payload)
        v = v.get("mode") if isinstance(v, dict) else v
    except Exception:  # noqa: BLE001
        v = payload
    mode = str(v).strip().lower()
    if mode not in ("auto", "heat", "cool", "off"):
        raise ValueError("mode must be auto|heat|cool|off")
    return mode


def _coerce_setpoint(payload: str) -> int:
    try:
        v = json.loads(payload)
        v = v.get("setpoint") if isinstance(v, dict) else v
    except Exception:  # noqa: BLE001
        v = payload
    sp = int(round(float(v)))
    if not 5 <= sp <= 30:
        raise ValueError("setpoint out of range 5-30")
    return sp


# ---- FastAPI ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    device.start()
    mqtt_layer.start()
    yield
    device.stop()


app = FastAPI(title="Nilan CTS600 API", version="1.0", lifespan=lifespan)


def auth(authorization: Optional[str] = Header(default=None)) -> None:
    if not API_TOKEN:
        return  # auth enforced at reverse proxy; direct token optional
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


class FanBody(BaseModel):
    level: int = Field(ge=0, le=4)


class ModeBody(BaseModel):
    mode: str


class TempBody(BaseModel):
    setpoint: int = Field(ge=5, le=30)


@app.get("/healthz")
def healthz():
    """Bus-aware liveness (TUE-100). Returns 503 once the bus has been
    disconnected for longer than HEALTH_STALE_SECONDS so the Docker healthcheck
    goes `unhealthy` and the autoheal sidecar restarts the container. Tolerates
    a single missed poll / brief reconnect blip (stays 200 while last good read
    is recent). Mockup mode has no real bus, so it is always healthy."""
    now = time.time()
    # Time since the last successful bus read; if we have never read, measure
    # from process start so a container that never connects also goes unhealthy.
    last_good = device._last_update_ts or START_TS
    stale_for = now - last_good
    bus_ok = MOCKUP or device._connected or (stale_for <= HEALTH_STALE_SECONDS)
    body = {
        "ok": bus_ok,
        "connected": device._connected,
        "mockup": MOCKUP,
        "read_only": READ_ONLY,
        "stale_for_s": round(stale_for, 1),
        "stale_threshold_s": HEALTH_STALE_SECONDS,
        "last_error": device._last_error,
    }
    return JSONResponse(status_code=200 if bus_ok else 503, content=body)


@app.get("/api/meta")
def get_meta(_: None = Depends(auth)):
    """Self-describing config + UI metadata. The dashboard reads this once to
    render labels/units, the current device configuration, and to know whether
    control (Phase 2) is unlocked (read_only=false + a live bus)."""
    return {
        "app_version": APP_VERSION,
        "mockup": MOCKUP,
        "read_only": READ_ONLY,
        "poll_seconds": POLL_SECONDS,
        "base_topic": MQTT_BASE,
        "esp_bridge": {"ip": ESP_IP or None, "port": ESP_PORT},
        "sensor_mapping": {
            "t_room": T_ROOM_KEY,
            "t_supply": T_SUPPLY_KEY,
            "t_exhaust": T_EXHAUST_KEY,
        },
        # Control envelope (Phase 2). Surfaced now so the UI can validate input
        # the moment writes are unlocked.
        "controls": {
            "fan": {"min": 0, "max": 4, "off_level": 0},
            "mode": ["auto", "heat", "cool", "off"],
            "setpoint": {"min": 5, "max": 30, "unit": "°C"},
        },
        # Friendly German labels for the curated fields + common raw keys.
        # Unknown raw keys fall back to the key name in the UI.
        "labels": {
            "t_room": "Raumtemperatur",
            "t_supply": "Zulufttemperatur",
            "t_exhaust": "Außen/Frischluft",
            "fan_level": "Lüfterstufe",
            "mode": "Betriebsmodus",
            "setpoint": "Solltemperatur",
            "status": "Status",
            "display": "Display-Text",
            # Sensor labels confirmed live from the unit's "ANZEIGE DATEN" menu
            # (TUE-55): RAUM=T15, ZULUFT=T2, FRISCHL.=T1, KONDENS.=T5, VERDAMP.=T6.
            "T15": "Raum (T15)", "T2": "Zuluft (T2)", "T1": "Frischluft/Außen (T1)",
            "T5": "Kondensator (T5)", "T6": "Verdampfer (T6)", "T7": "Nachheizregister (T7)",
            "ZULUFT_STUFE": "Zuluft-Stufe", "ABLUFT_STUFE": "Abluft-Stufe",
            "flow": "Lüfterstufe", "thermostat": "Solltemperatur",
            "program": "Programm",
            "humidity": "Luftfeuchte", "RH": "Luftfeuchte",
            "filter": "Filter", "led": "LED",
        },
        "units": {
            "t_room": "°C", "t_supply": "°C", "t_exhaust": "°C",
            "setpoint": "°C", "thermostat": "°C",
            "T15": "°C", "T1": "°C", "T2": "°C", "T5": "°C", "T6": "°C", "T7": "°C",
            "humidity": "%", "RH": "%",
        },
        # Plain-language meaning of each parameter, from the Nilan CTS600 manual +
        # frodef's reverse-engineering (TUE-55). Lets the UI explain values inline.
        "descriptions": {
            "T1": "Frischluft/Außentemperatur — angesaugte Außenluft (Fühler an der Nordseite).",
            "T2": "Zulufttemperatur am Ventilator, vor einem evtl. Nachheizregister (heißt T7 mit Heizregister).",
            "T3": "Frischluftfühler (an diesem Gerät nicht belegt).",
            "T4": "Gegenstrom-Wärmetauscher (an diesem Gerät nicht belegt).",
            "T5": "Kondensatortemperatur der Wärmepumpe.",
            "T6": "Verdampfertemperatur der Wärmepumpe.",
            "T7": "Zulufttemperatur nach einem Nachheizregister, falls am Gerät vorhanden.",
            "T15": "Fühler im CTS600-Bedienpanel. Das Panel ist durch den ESP ersetzt, "
                   "daher wird dieser Raumwert vom Daemon injiziert (Fallback) — KEIN echter "
                   "Live-Raumfühler. Für echte Raumtemperatur einen externen Fühler einspeisen.",
            "t_room": "Raumtemperatur, die das Gerät zur Regelung nutzt (= T15, aktuell injizierter Fallback).",
            "t_supply": "Zulufttemperatur (T2) — Luft, die in die Wohnung geblasen wird.",
            "t_exhaust": "Frischluft/Außen (T1) — angesaugte Außenluft.",
            "setpoint": "Solltemperatur (Thermostat), 5–30 °C — gewünschte Raumtemperatur.",
            "fan_level": "Lüfterstufe 0–4 (0 = aus, 4 = höchste).",
            "mode": "Betriebsmodus: auto (automatisch heizen/kühlen), heat, cool, off.",
            "status": "Aktueller Betriebszustand laut Gerät (z. B. KÜHLEN, HEIZEN).",
            "ZULUFT_STUFE": "Aktuelle Zuluft-Ventilatorstufe.",
            "ABLUFT_STUFE": "Aktuelle Abluft-Ventilatorstufe.",
            "program": "Aktives Zeitprogramm (Wochenprogramm), sofern gesetzt.",
            "led": "Betriebs-LED des Panels (an = Gerät läuft).",
        },
    }


@app.get("/api/status")
def get_status(_: None = Depends(auth)):
    return device.status()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Read-only web dashboard (TUE-55 Phase 1). Served same-origin so it lives
    behind the same Caddy auth as the API."""
    try:
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PlainTextResponse("dashboard.html missing from image", status_code=500)


def _device_write(fn, *args):
    """Run a device write, turning device/protocol failures into a clean 502."""
    if READ_ONLY:
        raise HTTPException(status_code=403, detail="device is in read-only bring-up mode")
    try:
        fn(*args)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("device write failed: %s", e)
        raise HTTPException(status_code=502, detail=f"device command failed: {e}")
    return device.status()


@app.post("/api/fan")
def post_fan(body: FanBody, _: None = Depends(auth)):
    return _device_write(device.set_fan, body.level)


@app.post("/api/mode")
def post_mode(body: ModeBody, _: None = Depends(auth)):
    m = body.mode.strip().lower()
    if m not in ("auto", "heat", "cool", "off"):
        raise HTTPException(status_code=422, detail="mode must be auto|heat|cool|off")
    return _device_write(device.set_mode, m)


@app.post("/api/temp")
def post_temp(body: TempBody, _: None = Depends(auth)):
    return _device_write(device.set_temp, body.setpoint)


if __name__ == "__main__":
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")
