#!/usr/bin/env bash
# Entrypoint for the nilan-api container.
#
# Real mode (NILAN_MOCKUP=0): start socat to bridge the ESP raw-TCP stream
#   (tcp:$ESP_IP:$ESP_PORT) to an in-container PTY at $NILAN_PORT, then launch
#   the API. Mockup mode (NILAN_MOCKUP=1): no socat; the wrapper uses CTS600Mockup.
#
# Supervision (TUE-100): three independent self-heal layers so a lost bus always
# recovers without a manual `docker restart`:
#   1. socat inner loop relaunches socat whenever socat itself exits (ESP reboot,
#      dropped TCP) — primary, in-place recovery, no container restart.
#   2. `wait -n`: if the socat supervisor loop OR uvicorn ever exits entirely
#      (the old bug: a backgrounded `set -e` subshell could silently die), THIS
#      process exits non-zero so Docker `restart: unless-stopped` recreates the
#      container with a fresh socat — secondary recovery.
#   3. A bus-aware /healthz (see nilan_api.py) goes 503 on sustained disconnect;
#      the autoheal sidecar restarts this container when socat is alive but the
#      bus is dead (half-open TCP) — the case the inner loop can't see.
# Run under tini (compose `init: true`) so signals propagate and socat zombies
# are reaped. NOTE: no top-level `set -e` — failures are handled explicitly so
# nothing can kill the supervisor by surprise.
set -uo pipefail

NILAN_PORT="${NILAN_PORT:-/dev/ttyNILAN}"
ESP_PORT="${ESP_PORT:-6638}"
MOCKUP="${NILAN_MOCKUP:-0}"

pids=()
terminate() {
  echo "entrypoint: received signal -> shutting down children" >&2
  kill "${pids[@]}" 2>/dev/null || true
  exit 143
}
trap terminate TERM INT

run_socat_supervisor() {
  # Never returns on its own. `set +e` guarantees a transient failure
  # (interrupted sleep, broken pipe on an echo) can NOT break out of the loop.
  set +e
  while true; do
    echo "socat: linking ${NILAN_PORT} <-> tcp:${ESP_IP}:${ESP_PORT}" >&2
    socat -d pty,link="${NILAN_PORT}",raw,echo=0,mode=666 "tcp:${ESP_IP}:${ESP_PORT}"
    rc=$?
    echo "socat: exited (rc=${rc}); reconnecting to ${ESP_IP}:${ESP_PORT} in 5s" >&2
    sleep 5
  done
}

if [ "$MOCKUP" = "0" ] && [ "$MOCKUP" != "false" ]; then
  if [ -z "${ESP_IP:-}" ]; then
    echo "FATAL: NILAN_MOCKUP=0 but ESP_IP is unset. Set ESP_IP (the live ESP bridge IP) or use NILAN_MOCKUP=1." >&2
    exit 1
  fi
  echo "Starting socat supervisor: $NILAN_PORT <-> tcp:${ESP_IP}:${ESP_PORT}"
  run_socat_supervisor &
  pids+=("$!")
  # wait for the PTY to appear before launching the API (best effort)
  for _ in $(seq 1 20); do [ -e "$NILAN_PORT" ] && break; sleep 0.5; done
fi

python -m uvicorn nilan_api:app --host "${NILAN_HTTP_HOST:-0.0.0.0}" --port "${NILAN_HTTP_PORT:-8642}" &
pids+=("$!")

# Block until ANY supervised child exits. If we get here the supervisor loop or
# uvicorn died, which must NEVER happen silently -> exit so Docker restarts us.
wait -n
ec=$?
echo "entrypoint: a supervised process exited (code ${ec}); exiting so Docker restarts the container" >&2
kill "${pids[@]}" 2>/dev/null || true
exit "${ec}"
