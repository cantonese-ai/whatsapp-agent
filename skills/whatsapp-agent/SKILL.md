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
- A phone with WhatsApp, and a cantonese.ai account (https://cantonese.ai).
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

## 3. Authenticate to cantonese.ai (pick one)

- **Sign in with cantonese.ai (recommended for personal use)**:

  ```bash
  docker compose run --rm voice-transcriber python app.py login
  ```

  It prints a URL. The user opens it in any browser, signs in and approves. The browser then goes to
  `http://127.0.0.1:<port>/callback?...`; that page usually fails to load because the login runs in
  a container, which is expected. The user copies the full address from the address bar and pastes
  it at the `Redirected URL:` prompt. The token is saved to the `transcriber-data` volume (mode
  0600) and refreshed automatically. Check with `docker compose run --rm voice-transcriber python app.py auth-status`.
- **API key**: the user creates an `sk-...` key at https://cantonese.ai/api-keys and puts it in
  `.env` as `CANTONESE_AI_API_KEY=`. Ask the user to paste it into the file themselves; do not echo
  it in commands or logs. The API key wins if both are configured.

## 4. Start

```bash
docker compose up -d
docker compose ps          # both services "healthy" after about a minute
docker compose logs -f voice-transcriber
```

## 5. Link the phone

Choose a session id (letters, digits, hyphens), e.g. `demo`.

```bash
skills/whatsapp-agent/scripts/pair.sh demo                 # saves qr-demo.png; scan it
skills/whatsapp-agent/scripts/pair.sh demo 85200000000     # or: pairing code for this number
```

On the phone: WhatsApp > Settings > Linked devices > Link a device. Scan the QR code, or choose
"Link with phone number instead" and type the 8-character code. The raw endpoints (all need the
`x-api-key: $WWEBJS_API_KEY` header, on `http://127.0.0.1:3000`):

- `GET /session/start/<id>`, `GET /session/status/<id>`, `GET /session/getSessions`
- `GET /session/qr/<id>/image` (PNG) or `GET /session/qr/<id>` (text)
- `POST /session/requestPairingCode/<id>` with `{"phoneNumber": "85200000000"}`
- `GET /session/terminate/<id>` unlinks and deletes the session
- A browser dashboard is at `http://127.0.0.1:3000/dashboard` (paste the key in its header box).

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

- `failed`: permanent STT error (e.g. 401/402 from cantonese.ai: bad key, not signed in, quota) or
  out of retries. Fix the cause, then retry.
- `reply_unknown`: the reply may have been sent already; it is never retried automatically, retry
  only after checking the chat.
- Not signed in / sign-in expired: jobs wait and retry; run the login again, then retry failed jobs.

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
  (the cantonese.ai token) private. Never print, commit or paste keys or tokens.
- Voice-note audio is sent to cantonese.ai for transcription. Tell everyone whose voice notes will be
  transcribed (for example, members of group chats) and respect their wishes.
- This uses WhatsApp Web automation, which WhatsApp's terms do not officially allow; the number can
  be restricted or banned. Keep the default reply pacing, avoid bulk use, and prefer a number the
  user can afford to lose.
- Rate limits: without a paid cantonese.ai plan, speech-to-text is limited to 10 requests per day,
  each up to 5 minutes of audio. Busy chats need a paid plan; consider limiting `VOICE_TYPES` or
  linking only the numbers that need it.
