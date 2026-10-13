#!/usr/bin/env bash
# Quick health check of a running agent: both services, every session's state, queue counts.
set -uo pipefail
. "$(dirname "$0")/common.sh"
ok=0
printf 'wwebjs-api ping:        '; api "$WWEBJS/ping" || ok=1; echo
printf 'voice-transcriber:      '; api "$TRANSCRIBER/" || ok=1; echo
printf 'sessions:               '; SESSIONS="$(api "$WWEBJS/session/getSessions")" || ok=1; echo "$SESSIONS"
for s in $(printf '%s' "$SESSIONS" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except ValueError: d={}
print(" ".join(x for x in (d.get("result") or d.get("sessions") or []) if isinstance(x, str)))' 2>/dev/null); do
  printf '  %-20s ' "$s"; api "$WWEBJS/session/status/$s"; echo
done
printf 'job counts:             '; api "$TRANSCRIBER/jobs?limit=1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("counts"))' || ok=1
printf 'recent failures:        '; api "$TRANSCRIBER/jobs?status=failed&limit=5" | python3 -c 'import json,sys
for j in json.load(sys.stdin).get("jobs", []): print("\n  ", j["id"], j["last_error"], end="")
print()' || ok=1
printf 'DNS (inside containers): '
(cd "$REPO_ROOT" && docker compose exec -T wwebjs-api getent hosts web.whatsapp.com >/dev/null 2>&1 \
  && docker compose exec -T voice-transcriber python -c "import socket; socket.gethostbyname('cantonese.ai')" >/dev/null 2>&1 \
  && echo ok) || { echo "FAILED -- set DNS_SERVER_1/2 in .env, see skills/whatsapp-agent/SKILL.md"; ok=1; }
printf 'cantonese.ai credential: '
(cd "$REPO_ROOT" && docker compose exec -T voice-transcriber python app.py auth-status 2>/dev/null) || echo "(docker compose not available here)"
exit $ok
