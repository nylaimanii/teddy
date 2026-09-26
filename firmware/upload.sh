#!/usr/bin/env bash
# Flash firmware/Bear.ino to the Uno.  Usage: firmware/upload.sh [port]
# (arduino-cli wants the sketch in a folder named after the file, so we stage
#  a copy in a temp dir rather than rearranging the repo.)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)}"
[ -n "$PORT" ] || { echo "no Arduino found -- plug it in or pass a port"; exit 1; }
STAGE="$(mktemp -d)/Bear"; mkdir -p "$STAGE"; cp Bear.ino "$STAGE/"
arduino-cli compile --fqbn arduino:avr:uno "$STAGE"
arduino-cli upload  --fqbn arduino:avr:uno -p "$PORT" "$STAGE"
echo "uploaded to $PORT"
