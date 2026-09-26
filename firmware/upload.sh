#!/usr/bin/env bash
# Flash firmware/Bear.ino to the Uno.  Usage: firmware/upload.sh [port]
#
# Retries, because this board regularly fails the first attempt with
# "not in sync: resp=0x00" and then drops off USB for a second before
# re-enumerating. A plain retry once the port is back almost always works.
# If it NEVER syncs, that's a power problem, not a serial one -- see body/README.md.
set -uo pipefail
cd "$(dirname "$0")"

find_port() {
  if [ -n "${1:-}" ]; then echo "$1"; else ls /dev/cu.usbmodem* 2>/dev/null | head -1; fi
}

PORT="$(find_port "${1:-}")"
[ -n "$PORT" ] || { echo "no Arduino found -- plug it in or pass a port"; exit 1; }

STAGE="$(mktemp -d)/Bear"; mkdir -p "$STAGE"; cp Bear.ino "$STAGE/"
arduino-cli compile --fqbn arduino:avr:uno "$STAGE" || exit 1

for attempt in 1 2 3 4; do
  # wait for the port to come back if a previous attempt knocked it off USB
  for _ in $(seq 20); do [ -e "$PORT" ] && break; sleep 0.5; PORT="$(find_port "${1:-}")"; done
  echo "--- upload attempt $attempt on $PORT ---"
  if arduino-cli upload --fqbn arduino:avr:uno -p "$PORT" "$STAGE"; then
    echo "uploaded to $PORT"
    exit 0
  fi
  sleep 2
done

echo
echo "FAILED after 4 attempts. If it never syncs, suspect power:"
echo "  8 SG90s cannot run off the Uno's USB 5V rail (500mA polyfuse)."
echo "  Give the servos their own 4xAA pack, grounds tied together."
exit 1
