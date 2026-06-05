#!/usr/bin/env bash
# Entrypoint for the nilan-api container.
#
# Real mode (NILAN_MOCKUP=0): start socat to bridge the ESP raw-TCP stream
#   (tcp:$ESP_IP:$ESP_PORT) to an in-container PTY at $NILAN_PORT, then launch
#   the API. socat is supervised in a restart loop so an ESP reboot/disconnect
#   self-heals without taking the container down.
# Mockup mode (NILAN_MOCKUP=1): no socat; the wrapper uses CTS600Mockup.
set -euo pipefail

NILAN_PORT="${NILAN_PORT:-/dev/ttyNILAN}"
ESP_PORT="${ESP_PORT:-6638}"
MOCKUP="${NILAN_MOCKUP:-0}"

if [ "$MOCKUP" = "0" ] && [ "$MOCKUP" != "false" ]; then
  if [ -z "${ESP_IP:-}" ]; then
    echo "FATAL: NILAN_MOCKUP=0 but ESP_IP is unset. Set ESP_IP (the live ESP bridge IP) or use NILAN_MOCKUP=1." >&2
    exit 1
  fi
  echo "Starting socat tunnel: $NILAN_PORT <-> tcp:${ESP_IP}:${ESP_PORT}"
  (
    while true; do
      socat -d pty,link="${NILAN_PORT}",raw,echo=0,mode=666 "tcp:${ESP_IP}:${ESP_PORT}" || true
      echo "socat exited; reconnecting to ${ESP_IP}:${ESP_PORT} in 5s" >&2
      sleep 5
    done
  ) &
  # wait for the PTY to appear
  for _ in $(seq 1 20); do [ -e "$NILAN_PORT" ] && break; sleep 0.5; done
fi

exec python -m uvicorn nilan_api:app --host "${NILAN_HTTP_HOST:-0.0.0.0}" --port "${NILAN_HTTP_PORT:-8642}"
