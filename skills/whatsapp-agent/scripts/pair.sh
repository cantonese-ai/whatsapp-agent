#!/usr/bin/env bash
# Link a WhatsApp number to the agent.
#   pair.sh <session-id>                      start the session and save the QR code to qr-<session-id>.png
#   pair.sh <session-id> <phone-number>       get an 8-character pairing code instead (international
#                                             format, digits only, e.g. 85200000000)
# Then on the phone: WhatsApp > Settings > Linked devices > Link a device (scan the QR, or choose
# "Link with phone number instead" and type the code).
set -euo pipefail
. "$(dirname "$0")/common.sh"
SESSION="${1:?usage: pair.sh <session-id> [phone-number]}"
PHONE="${2:-}"
case "$SESSION" in *[!A-Za-z0-9-]*) echo "session id: letters, digits and hyphens only" >&2; exit 1;; esac

echo "Starting session '$SESSION'..."
api "$WWEBJS/session/start/$SESSION"; echo
sleep 5

if [ -n "$PHONE" ]; then
  echo "Requesting a pairing code for +$PHONE..."
  api -X POST -H 'content-type: application/json' \
    -d "{\"phoneNumber\": \"$PHONE\", \"showNotification\": true}" \
    "$WWEBJS/session/requestPairingCode/$SESSION"; echo
else
  OUT="qr-$SESSION.png"
  for _ in $(seq 1 12); do
    if api -f -o "$OUT" "$WWEBJS/session/qr/$SESSION/image" 2>/dev/null && [ -s "$OUT" ]; then
      echo "QR code saved to $OUT - open it and scan it from the phone (it refreshes about every 20 s;"
      echo "run this again if it expired). Or open $WWEBJS/dashboard in a browser."
      break
    fi
    sleep 5
  done
fi

echo "Waiting for the link (Ctrl-C to stop)..."
for _ in $(seq 1 60); do
  STATUS="$(api "$WWEBJS/session/status/$SESSION" || true)"
  case "$STATUS" in *CONNECTED*) echo "Linked: $STATUS"; exit 0;; esac
  sleep 5
done
echo "Not linked yet. Current status: $STATUS" >&2
exit 1
