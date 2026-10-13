# Shared by pair.sh and health.sh. Reads WWEBJS_API_KEY and the ports from the environment or
# from the repository's .env (never prints the key).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)"
env_value() {
  local name="$1" value="${!1:-}"
  if [ -z "$value" ] && [ -f "$REPO_ROOT/.env" ]; then
    value="$(grep -E "^${name}=" "$REPO_ROOT/.env" | tail -n 1 | cut -d= -f2-)"
  fi
  printf '%s' "$value"
}
WWEBJS_API_KEY="$(env_value WWEBJS_API_KEY)"
WWEBJS_PORT="$(env_value WWEBJS_PORT)"; WWEBJS_PORT="${WWEBJS_PORT:-3000}"
TRANSCRIBER_PORT="$(env_value TRANSCRIBER_PORT)"; TRANSCRIBER_PORT="${TRANSCRIBER_PORT:-8080}"
WWEBJS="${WWEBJS_URL_FROM_HOST:-http://127.0.0.1:${WWEBJS_PORT}}"
TRANSCRIBER="${TRANSCRIBER_URL_FROM_HOST:-http://127.0.0.1:${TRANSCRIBER_PORT}}"
if [ -z "$WWEBJS_API_KEY" ]; then
  echo "WWEBJS_API_KEY is not set (in the environment or $REPO_ROOT/.env)" >&2
  return 1 2>/dev/null || exit 1
fi
# Local endpoints only: never send these requests through a proxy.
api() { curl -sS --noproxy '*' -H "x-api-key: ${WWEBJS_API_KEY}" "$@"; }
