---
name: whatsapp-agent
description: Set up, operate, update or troubleshoot a self-hosted WhatsApp agent that transcribes Cantonese (and other) voice notes with the cantonese.ai speech-to-text API and replies in the same chat. Use when someone wants WhatsApp voice notes turned into text automatically on their own machine or server, needs to link (pair) a phone to it, sign it in with a cantonese.ai account or API key, check its health, retry failed transcriptions, or upgrade it.
---

# whatsapp-agent

Two containers from this repository (https://github.com/cantonese-ai/whatsapp-agent):

- `wwebjs-api`: WhatsApp Web in headless Chromium with a REST API. It is linked to the user's
  phone like WhatsApp Desktop and controls that WhatsApp account.
- `voice-transcriber`: receives voice notes from `wwebjs-api`, sends the audio to
  `https://cantonese.ai/api/stt`, and replies in the same chat quoting the voice note
  (`📝 <transcript>`). Jobs are kept in a SQLite queue, so nothing is lost on a restart.

Run commands from the repository root. Helper scripts are in `skills/whatsapp-agent/scripts/`.

## 1. Prerequisites

- Docker with the Compose plugin (`docker compose version`), about 1 GB of free RAM per linked number.
- A phone with WhatsApp, and a cantonese.ai account (https://cantonese.ai) with a speech-to-text plan.
- The `whatsapp-agent` OAuth client secret for this install (provided by cantonese.ai / the setup
  bot), unless the user prefers an API key.
- `curl` and `openssl` on the host.

## 2. Install and configure

```bash
git clone https://github.com/cantonese-ai/whatsapp-agent.git
cd whatsapp-agent
cp .env.example .env
chmod 600 .env
# Shared secret between the two services. Generate it; never reuse one from elsewhere.
sed -i.bak "s/^WWEBJS_API_KEY=.*/WWEBJS_API_KEY=$(openssl rand -hex 32)/" .env && rm .env.bak
```

Edit `.env` only for what the user asks for (language `STT_LANG`, alert e-mails, ports). Every
variable is documented in `.env.example`.

## 3. Authenticate to cantonese.ai

Usage is billed to the cantonese.ai account that signs in, which needs a speech-to-text plan.

- **Sign in with cantonese.ai (normal)**: the user signs in on the normal cantonese.ai login page.
  1. Put the OAuth client secret you were given for this install (by cantonese.ai / the setup bot)
     into `.env` as `CANTONESE_AI_OAUTH_CLIENT_SECRET=` by editing the file. It goes only into
     `.env`: never into the repository, a commit, chat messages, logs or issues, and never on a
     command line or in `echo` output. Do not change `CANTONESE_AI_OAUTH_CLIENT_ID` (`whatsapp-agent`)
     or `CANTONESE_AI_OAUTH_CALLBACK_PORT` (`53682`, the only registered redirect URI port).
  2. Run the login:

     ```bash
     docker compose run --rm -p 127.0.0.1:53682:53682 -e CANTONESE_AI_OAUTH_CALLBACK_BIND=0.0.0.0 \
       voice-transcriber python app.py login --no-browser
     ```

     It prints a URL. The user opens it in a browser on the same machine, signs in and approves. The
     browser goes to `http://127.0.0.1:53682/callback?...`, which is forwarded into the container,
     and the login finishes by itself. If the browser is on another computer, that page will not
     load: the user copies the full address from the address bar and pastes it at the
     `Redirected URL:` prompt. If port 53682 is already in use, stop whatever holds it (often an
     earlier login run) and run the login again.
  3. The token is saved to the `transcriber-data` volume (mode 0600) and refreshed automatically.
     Check with `docker compose run --rm voice-transcriber python app.py auth-status` (it shows
     whether a client secret is configured, never the secret itself).
- **API key (optional alternative)**: the user creates an `sk-...` key at
  https://cantonese.ai/api-keys and puts it in `.env` as `CANTONESE_AI_API_KEY=`. Ask the user to
  paste it into the file themselves; do not echo it in commands or logs. No client secret is needed
  then. The API key wins if both are configured.

## 4. Start

```bash
docker compose up -d
docker compose ps          # both services "healthy" after about a minute
docker compose logs -f voice-transcriber
```

Check that the containers can resolve WhatsApp **before** pairing (no QR code appears otherwise):

```bash
docker compose exec -T wwebjs-api getent hosts web.whatsapp.com   # must print an address
```

Both containers use the DNS servers in `.env` (`DNS_SERVER_1` / `DNS_SERVER_2`, public resolvers by
default), so this normally works out of the box. If it prints nothing, outbound DNS is probably blocked:
set the network's own resolvers in `.env`, run `docker compose up -d --force-recreate` and check again.
Do not restart the Docker daemon or edit `/etc/docker/daemon.json` on a shared server.
A session started while DNS was broken can stay stuck: `GET /session/terminate/<id>`, then pair again.

## 5. Link the phone (pairing code -- ask for the user's phone number)

Link with a **pairing code**, not a QR code: the user usually talks to you from the same phone they are
linking, so they cannot scan a code shown on that screen.

1. **Ask the user for their WhatsApp phone number** with country code (e.g. Hong Kong `+852 xxxx xxxx`).
   Normalise it to digits only, no `+`, spaces or leading zeros: `85200000000`. Use it only for pairing;
   don't store it anywhere else.
2. Pick a session id (letters, digits, hyphens), e.g. `demo`, and request the code:
   ```bash
   skills/whatsapp-agent/scripts/pair.sh demo 85200000000    # prints an 8-character code, e.g. ABCD-EFGH
   ```
3. **Send the user the code with these steps:** open WhatsApp on that phone > Settings (or the
   three-dot menu) > Linked devices > Link a device > **Link with phone number instead** > enter the
   8-character code. WhatsApp also shows a notification on the phone. The code expires after a few
   minutes; if it does, request a new one (step 2).
4. Wait until `GET /session/status/demo` reports connected (`pair.sh` polls it), then tell the user it
   is linked.

Only if pairing codes fail repeatedly, fall back to a QR code (`pair.sh demo` without a number saves
`qr-demo.png`) and ask the user to scan it from **another** device's screen.

Raw endpoints (all need the `x-api-key: $WWEBJS_API_KEY` header, on `http://127.0.0.1:3000`):

- `GET /session/start/<id>`, `GET /session/status/<id>`, `GET /session/getSessions`
- `POST /session/requestPairingCode/<id>` with `{"phoneNumber": "85200000000", "showNotification": true}`
- `GET /session/qr/<id>/image` (PNG) or `GET /session/qr/<id>` (text) -- fallback only
- `GET /session/terminate/<id>` unlinks and deletes the session

The session survives restarts (`wwebjs-sessions` volume). Several numbers can be linked, one session each.

## 6. Verify

Ask the user to send a short voice note to any chat, or to themselves ("Message yourself"). Within
a few seconds a reply quoting it should appear: `📝 <transcript>`. If not, run
`skills/whatsapp-agent/scripts/health.sh` and read `docker compose logs voice-transcriber`.

## 7. Operate

The admin API on `http://127.0.0.1:8080` needs the same `x-api-key` header:

```bash
. skills/whatsapp-agent/scripts/common.sh
api "$TRANSCRIBER/jobs?status=failed"                        # failed | reply_unknown | pending | processing | done | skipped
api -X POST -d '{"status": "failed"}' "$TRANSCRIBER/jobs/retry"     # re-queue all failed jobs
api -X POST -d '{"ids": ["<message id>"]}' "$TRANSCRIBER/jobs/retry"
api "$TRANSCRIBER/notifications"                             # numbers linked / unlinked
```

- `failed`: permanent STT error (e.g. 401/402 from cantonese.ai: bad key, not signed in, no plan) or
  out of retries. Fix the cause, then retry.
- `reply_unknown`: the reply may have been sent already; it is never retried automatically, retry
  only after checking the chat.
- Not signed in / sign-in expired: jobs wait and retry; run the login again, then retry failed jobs.
- `invalid_client` / "client secret is not set": `CANTONESE_AI_OAUTH_CLIENT_SECRET` in `.env` is
  missing or wrong; fix it (without printing it), `docker compose up -d`, then sign in again.

## 8. Update

```bash
git pull
docker compose pull
docker compose up -d
```

`wwebjs-api/sessions.js` is a patched copy of one file from a specific `wwebjs-api` image version.
Only change the image tag in `docker-compose.yml` together with a matching `sessions.js`.

## Safety rules

- Never expose `wwebjs-api` (port 3000) or the transcriber (port 8080) to the internet or a shared
  network. Both are bound to 127.0.0.1; use an SSH tunnel for remote access. Whoever reaches
  `wwebjs-api` can read and send messages as the linked number.
- Keep `.env`, the `wwebjs-sessions` volume (the WhatsApp login) and the `transcriber-data` volume
  (the cantonese.ai token) private. Never print, commit or paste keys, tokens or the OAuth client
  secret.
- Voice-note audio is sent to cantonese.ai for transcription. Tell everyone whose voice notes will be
  transcribed (for example, members of group chats) and respect their wishes.
- This uses WhatsApp Web automation, which WhatsApp's terms do not officially allow; the number can
  be restricted or banned. Keep the default reply pacing, avoid bulk use, and prefer a number the
  user can afford to lose.
- Billing: every transcribed voice note is one speech-to-text request billed to the signed-in
  cantonese.ai account, which needs a speech-to-text plan. For busy chats consider limiting
  `VOICE_TYPES` or linking only the numbers that need it.
