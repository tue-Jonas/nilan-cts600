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

from fastapi import FastAPI, HTTPException, Header, Depends
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
T15_FALLBACK = float(os.environ.get("NILAN_T15_FALLBACK", "21"))
API_TOKEN = os.environ.get("NILAN_API_TOKEN", "").strip()  # optional bearer for direct access
READ_ONLY = os.environ.get("NILAN_READ_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
HTTP_HOST = os.environ.get("NILAN_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("NILAN_HTTP_PORT", "8642"))

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
            except Exception as e:  # noqa: BLE001
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
        if self._on_state_change:
            try:
                self._on_state_change(self.status())
            except Exception as e:  # noqa: BLE001
                log.warning("state callback failed: %s", e)
        return data

    def _retry(self, fn, *args):
        last = None
        for attempt in range(1, RETRIES + 1):
            try:
                return fn(*args)
            except (NilanCTS600Exception, TimeoutError) as e:
                last = e
                log.warning("device op %s attempt %d/%d failed: %s",
                            getattr(fn, "__name__", fn), attempt, RETRIES, e)
                time.sleep(0.3)
        raise last if last else RuntimeError("device op failed")

    def set_fan(self, level: int) -> None:
        with self._lock:
            if level == 0:
                self._retry(self._cts.key_off)
            else:
                # ensure unit is on, then set flow 1-4
                try:
                    self._retry(self._cts.key_on)
                except Exception:  # noqa: BLE001
                    pass
                self._retry(self._cts.setFlow, level)
        self.refresh()

    def set_mode(self, mode: str) -> None:
        with self._lock:
            if mode == "off":
                self._retry(self._cts.key_off)
            else:
                try:
                    self._retry(self._cts.key_on)
                except Exception:  # noqa: BLE001
                    pass
                self._retry(self._cts.setMode, MODE_TO_FRODEF[mode])
        self.refresh()

    def set_temp(self, setpoint: int) -> None:
        with self._lock:
            self._retry(self._cts.setThermostat, setpoint)
        self.refresh()

    # -- contract projection (plan section 8) --
    def status(self) -> dict[str, Any]:
        d = self._snapshot
        raw_mode = str(d.get("mode", "")).lower() or None
        status_txt = str(d.get("status", "")).upper()
        # 'off' is inferred when the unit reports an OFF/standby status
        if status_txt in ("OFF", "STANDBY", "STOP"):
            mode = "off"
        else:
            mode = raw_mode

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
            "connected": self._connected,
            "mockup": MOCKUP,
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
    return {"ok": True, "connected": device._connected, "mockup": MOCKUP, "read_only": READ_ONLY}


@app.get("/api/status")
def get_status(_: None = Depends(auth)):
    return device.status()


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
