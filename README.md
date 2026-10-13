# whatsapp-agent

A self-hosted WhatsApp agent that transcribes voice notes with the
[cantonese.ai](https://cantonese.ai) speech-to-text API and replies in the same chat, quoting the
voice note:

> 🎤 *(voice note, 0:12)*
> ↳ 📝 今日下晝三點喺銅鑼灣見，記得帶埋份文件。

It is built for Cantonese, and works for the other languages the cantonese.ai API supports
(`STT_LANG`). You link your own WhatsApp number to it like WhatsApp Desktop, and sign in with your
cantonese.ai account (or, optionally, use an API key). Voice notes you send and receive are
transcribed, in direct chats and groups. Usage is billed to the signed-in cantonese.ai account,
which needs a speech-to-text plan.

## How it works

```
 your phone ──(linked device)── WhatsApp
                                   │
              ┌────────────────────┴───────────────────┐  docker compose, on your host
              │  wwebjs-api  (WhatsApp Web, headless   │  127.0.0.1:3000 only
              │  Chromium, REST API, patched sessions) │
              └───────┬─────────────────────▲──────────┘
       webhook "media"│ (voice note audio)  │ POST /client/sendMessage (reply, quoting the note)
                      ▼                     │
              ┌─────────────────────────────┴──────────┐
              │  voice-transcriber  (Python, stdlib)   │  127.0.0.1:8080 only
              │  SQLite queue, retries, pacing, dedupe │  GET /jobs, POST /jobs/retry
              └───────┬────────────────────────────────┘
                      │ HTTPS, Authorization: Bearer <API key or signed-in token>
                      ▼
              https://cantonese.ai/api/stt
```

- **wwebjs-api** is the stock [`avoylenko/wwebjs-api`](https://github.com/avoylenko/wwebjs-api)
  image with one file, [`wwebjs-api/sessions.js`](wwebjs-api/sessions.js), mounted over it. The
  patch forwards your *own* voice notes too, catches up on notes received while it was offline,
  reports when a number is linked or unlinked, and survives a pairing-code crash.
- **voice-transcriber** ([`voice-transcriber/app.py`](voice-transcriber/app.py)) stores each voice
  note in a SQLite queue before acknowledging the webhook, transcribes it, and replies. Failures are
  retried with backoff; replies are paced (one per 5 s per number) so the account does not look
  like spam; if another agent already posted a transcript of the same note in a chat, it does not
  post a second one.

## Quick start

Requirements: Docker with Compose, a phone with WhatsApp, a [cantonese.ai](https://cantonese.ai)
account with a speech-to-text plan, and the `whatsapp-agent` OAuth client secret (provided by
cantonese.ai / the setup bot; not needed if you use an API key instead).

```bash
git clone https://github.com/cantonese-ai/whatsapp-agent.git
cd whatsapp-agent
cp .env.example .env && chmod 600 .env

# 1. A shared secret between the two services (also protects both admin APIs).
#    Generate your own; anyone holding it controls the linked WhatsApp account.
sed -i.bak "s/^WWEBJS_API_KEY=.*/WWEBJS_API_KEY=$(openssl rand -hex 32)/" .env && rm .env.bak

# 2. Put the client secret you were given into .env (CANTONESE_AI_OAUTH_CLIENT_SECRET=...) with an
#    editor, then sign in with your cantonese.ai account: open the printed URL in a browser on
#    this machine. (Or set CANTONESE_AI_API_KEY in .env instead.)
docker compose run --rm -p 127.0.0.1:53682:53682 -e CANTONESE_AI_OAUTH_CALLBACK_BIND=0.0.0.0 \
  voice-transcriber python app.py login --no-browser

# 3. Start.
docker compose up -d

# 4. Link your phone (session id "demo"): scan qr-demo.png from WhatsApp > Linked devices.
skills/whatsapp-agent/scripts/pair.sh demo
```

Send yourself a voice note ("Message yourself"); a `📝` reply should follow within seconds.

## Sign in with cantonese.ai

Signing in uses the normal cantonese.ai login page, like cantonese.ai's other apps (OAuth 2.0
authorization code flow, client id `whatsapp-agent`). The agent authenticates itself to
cantonese.ai with a client secret, `CANTONESE_AI_OAUTH_CLIENT_SECRET`, which cantonese.ai / the
setup bot gives you. Keep it in `.env` only: never in the repository, chat logs or issues. It is
needed for the sign-in and for every token refresh.

1. `python app.py login` prints a sign-in URL (and opens a browser when it can; `--no-browser`
   only prints it).
2. You sign in at cantonese.ai and approve access (scope `user:inference`).
3. The browser is sent to `http://127.0.0.1:53682/callback`, the only registered redirect URI, so
   the port is fixed. The agent listens there and finishes by itself. With Docker the command in
   Quick start publishes `127.0.0.1:53682` into the container for the login run only, and tells
   the listener to bind `0.0.0.0` inside it (`CANTONESE_AI_OAUTH_CALLBACK_BIND`). If port 53682 is
   busy the login stops with an error instead of picking another port. When the browser is on
   another computer, that page will not load: copy the full URL from the address bar and paste it
   at the prompt (`--no-listen` skips the listener and only waits for the paste).
4. The access token, refresh token and expiry are saved to `/data/cantonese-ai-token.json` (file
   mode 0600, on the `transcriber-data` volume). The token is refreshed before it expires and after
   a 401; refresh tokens rotate and the new one is always saved. Neither the tokens nor the client
   secret are ever logged or written to the token file.

Other commands: `python app.py auth-status` (which credential is used and whether a client secret
is configured, without showing either) and `python app.py logout`.

**Optional: API key.** Instead of signing in, set `CANTONESE_AI_API_KEY` to an `sk-...` key from
https://cantonese.ai/api-keys; no client secret is needed then. If both are set, the API key is
used.

While neither credential works, voice notes stay queued and are retried; after signing in again,
re-queue anything that ended up failed (see Operating).

## Linking a phone

If no QR code ever appears, first check DNS inside the containers:
`docker compose exec -T wwebjs-api getent hosts web.whatsapp.com`. If that prints nothing, add a
`docker-compose.override.yml` with `dns: [1.1.1.1, 8.8.8.8]` for both services and run
`docker compose up -d --force-recreate` (details in `skills/whatsapp-agent/SKILL.md`).

All wwebjs-api endpoints need the header `x-api-key: <WWEBJS_API_KEY>` and are on
`http://127.0.0.1:3000` (paths checked against wwebjs-api v1.34.7-hotfix2):

| Step | Request |
| --- | --- |
| Start a session | `GET /session/start/<id>` (id: letters, digits, hyphens) |
| QR code | `GET /session/qr/<id>/image` (PNG), or `GET /session/qr/<id>` |
| Or a pairing code | `POST /session/requestPairingCode/<id>` with `{"phoneNumber": "85200000000"}` |
| State | `GET /session/status/<id>`, `GET /session/getSessions` |
| Unlink and delete | `GET /session/terminate/<id>` |

Then on the phone: WhatsApp > Settings > Linked devices > Link a device (scan the QR code, or
"Link with phone number instead" and enter the code). The web dashboard at
`http://127.0.0.1:3000/dashboard` does the same in a browser; Swagger UI is at `/api-docs`.
`skills/whatsapp-agent/scripts/pair.sh <id> [phone]` wraps these steps.

Each session runs its own Chromium (about 300–500 MB). By default every session posts to
`BASE_WEBHOOK_URL` (the transcriber). To send one session's events elsewhere, set
`<SESSIONID>_WEBHOOK_URL` (session id in upper case, e.g. `DEMO_WEBHOOK_URL`) in the `wwebjs-api`
environment in `docker-compose.yml`.

## Configuration

All variables are documented in [`.env.example`](.env.example). The main ones:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WWEBJS_API_KEY` | (required) | Shared secret for wwebjs-api, its webhooks and the transcriber's admin API. `openssl rand -hex 32`. |
| `CANTONESE_AI_OAUTH_CLIENT_ID` | `whatsapp-agent` | OAuth client id for the sign-in. |
| `CANTONESE_AI_OAUTH_CLIENT_SECRET` | empty | OAuth client secret, provided by cantonese.ai / the setup bot. Keep private. Needed for sign-in and refresh. |
| `CANTONESE_AI_OAUTH_CALLBACK_PORT` | `53682` | Callback port; must match the registered redirect URI `http://127.0.0.1:53682/callback`. |
| `CANTONESE_AI_OAUTH_CALLBACK_BIND` | `127.0.0.1` | Listener address for the callback; `0.0.0.0` only for the Docker login run. |
| `CANTONESE_AI_API_KEY` | empty | Optional `sk-...` key from https://cantonese.ai/api-keys instead of signing in. Wins over the sign-in. |
| `CANTONESE_AI_ISSUER` | `https://cantonese.ai` | Authorization server (`/api/oauth/authorize`, `/api/oauth/token`). |
| `CANTONESE_AI_TOKEN_PATH` | `/data/cantonese-ai-token.json` | Saved sign-in (0600). |
| `STT_URL` | `https://cantonese.ai/api/stt` | Speech-to-text endpoint. |
| `STT_LANG` | `cantonese` | Language sent to the API. |
| `VOICE_TYPES` | `ptt,audio` | Voice notes and forwarded audio files. |
| `REPLY_PREFIX` | `📝 ` | Prefix of each reply. |
| `SEND_MIN_INTERVAL_S` | `5` | Minimum seconds between replies per number. |
| `MAX_ATTEMPTS`, `BACKOFF_BASE_S`, `BACKOFF_MAX_S` | `10`, `2`, `900` | Retry policy (about an hour in total). |
| `CATCHUP_MAX_SECONDS`, `CATCHUP_MAX_AGE_S` | `3600` | How far back notes missed during a restart are transcribed. |
| `SENDGRID_API_KEY`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM` | empty | Optional e-mail when a number is linked or unlinked. Off unless all three are set. |
| `WWEBJS_PORT`, `TRANSCRIBER_PORT` | `3000`, `8080` | Host ports, bound to 127.0.0.1. |

## Operating

The transcriber's admin API (same `x-api-key` header) on `http://127.0.0.1:8080`:

```bash
KEY=$(grep ^WWEBJS_API_KEY= .env | cut -d= -f2-)
curl -s -H "x-api-key: $KEY" "http://127.0.0.1:8080/jobs?status=failed"
curl -s -H "x-api-key: $KEY" -X POST -d '{"status": "failed"}' http://127.0.0.1:8080/jobs/retry
curl -s -H "x-api-key: $KEY" -X POST -d '{"ids": ["<message id>"]}' http://127.0.0.1:8080/jobs/retry
curl -s -H "x-api-key: $KEY" http://127.0.0.1:8080/notifications
```

Job states: `pending`, `processing`, `done`, `failed` (permanent error such as 401/402 from the API,
or out of attempts), `skipped` (a transcript was already in the chat, or a catch-up note was too
old) and `reply_unknown` (the reply may have been sent; never retried automatically).
`skills/whatsapp-agent/scripts/health.sh` prints an overview.

Update with `git pull && docker compose pull && docker compose up -d`. The patched `sessions.js`
belongs to one wwebjs-api version; change the image tag only together with it.

## Billing

Every transcribed voice note is one speech-to-text request, billed to the signed-in cantonese.ai
account (or the account that owns the API key). That account needs a speech-to-text plan; see
https://cantonese.ai for plans. Without one the API answers 401/402 and jobs end up `failed`.

## Privacy and safety

- **What leaves your host:** the audio of each voice note is sent to cantonese.ai for
  transcription (plus, if you enable alert e-mails, a short message to SendGrid). Message text,
  contacts and everything else stay on your host, apart from WhatsApp itself. The transcript is
  posted back into the chat, so everyone in that chat sees it.
- Tell the people whose voice notes you transcribe, especially in groups.
- Never expose port 3000 or 8080 publicly; use an SSH tunnel for remote access. The
  `wwebjs-sessions` volume is your WhatsApp login and `transcriber-data` holds your cantonese.ai
  token: keep both, and `.env`, private.
- This automates WhatsApp Web, which WhatsApp's terms do not officially allow. Your number may be
  restricted. Keep the pacing defaults and do not use it for bulk messaging.

## Kubernetes

Optional manifests are in [`k8s/`](k8s/) (namespace `whatsapp-agent`, Secrets referenced by name;
create them as described at the top of each manifest; the OAuth client id and secret come from the
optional Secret `whatsapp-agent-oauth-client`). In the login below, paste the redirected URL at the
prompt:

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s | kubectl apply -f -
kubectl -n whatsapp-agent exec -it deploy/voice-transcriber -- python /app/app.py login --no-browser
kubectl -n whatsapp-agent port-forward svc/wwebjs-api 3000:3000
```

## For AI agents

[`skills/whatsapp-agent/SKILL.md`](skills/whatsapp-agent/SKILL.md) is an agent skill (Claude Code
and similar) with step-by-step instructions to host and operate an instance.

## Development

The transcriber uses only the Python standard library (3.10+). Tests use fake servers and need no
network or credentials:

```bash
cd voice-transcriber
python3 -m unittest -v
```

## License

MIT, see [LICENSE](LICENSE). `wwebjs-api/sessions.js` is a modified copy of a file from
wwebjs-api (MIT, Copyright (c) 2024 Anton Voylenko); see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Not affiliated with WhatsApp or Meta.
