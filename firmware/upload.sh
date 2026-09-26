#!/usr/bin/env bash
# Flash firmware/Bear/Bear.ino to the Uno.  Usage: firmware/upload.sh [port]
#
# The sketch lives in firmware/Bear/ because the Arduino toolchain requires the
# folder name to match the .ino -- so you can also just open firmware/Bear in
# the Arduino IDE and hit upload.
#
# We retry: this board sometimes fails the first attempt with "not in sync:
# resp=0x00" and drops off USB for a second before re-enumerating. A failed
# attempt can also leave an orphaned avrdude holding the port, which makes the
# NEXT attempt fail for a different reason than the first -- so we clear those
# out before starting. If it never syncs, check the wiring and the servo power
# (see body/README.md); it is not usually the sketch.
set -uo pipefail
cd "$(dirname "$0")"

SKETCH="$PWD/Bear"
[ -f "$SKETCH/Bear.ino" ] || { echo "missing $SKETCH/Bear.ino"; exit 1; }

find_port() {
  if [ -n "${1:-}" ]; then echo "$1"; else ls /dev/cu.usbmodem* 2>/dev/null | head -1; fi
}

PORT="$(find_port "${1:-}")"
[ -n "$PORT" ] || { echo "no Arduino found -- plug it in or pass a port"; exit 1; }

# Clear any avrdude left holding the port by an earlier failed upload.
if pgrep -x avrdude >/dev/null 2>&1; then
  echo "clearing stale avrdude processes holding the port"
  pkill -9 -x avrdude; sleep 1
fi

arduino-cli compile --fqbn arduino:avr:uno "$SKETCH" || exit 1

for attempt in 1 2 3 4; do
  for _ in $(seq 20); do [ -e "$PORT" ] && break; sleep 0.5; PORT="$(find_port "${1:-}")"; done
  echo "--- upload attempt $attempt on $PORT ---"
  if arduino-cli upload --fqbn arduino:avr:uno -p "$PORT" "$SKETCH"; then
    echo "uploaded to $PORT"
    exit 0
  fi
  pkill -9 -x avrdude 2>/dev/null
  sleep 2
done

echo
echo "FAILED after 4 attempts. Check wiring and servo power before re-flashing;"
echo "see the power section in body/README.md."
exit 1
