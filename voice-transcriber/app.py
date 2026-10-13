"""Webhook receiver for wwebjs-api: transcribe WhatsApp voice notes with the cantonese.ai
speech-to-text API and reply in the same chat, quoting the voice note.

  python app.py                      run the service (webhook + admin API on PORT, default 8080)
  python app.py login [--no-browser] sign in with your cantonese.ai account (OAuth 2.0 + PKCE)
  python app.py logout               forget the saved sign-in
  python app.py auth-status          which cantonese.ai credential is in use (never prints it)

Voice notes go into a SQLite queue (on a volume) before the webhook is acknowledged, so
failed or interrupted jobs survive restarts and can be listed and re-queued:

  GET  /jobs?status=failed|reply_unknown|pending|processing|done[&limit=N]
  POST /jobs/retry  {"ids": ["<msg id>", ...]}  or  {"status": "failed"}

  GET  /notifications[?limit=N]   pairing / removal e-mails and their delivery state

All need the x-api-key header (the wwebjs-api key).

The patched wwebjs-api also posts session_paired / session_removed events when a phone number
is linked or unlinked. Each is recorded under /notifications and, only if SENDGRID_API_KEY,
ALERT_EMAIL_TO and ALERT_EMAIL_FROM are all set, e-mailed from a queue with the same retry and
lease rules as voice notes. Alert e-mails are off by default.

Job states:
  pending        waiting for its next attempt (next_at)
  processing     claimed by a worker (leased; taken over only if the lease runs out)
  done           replied
  failed         permanent STT error (4xx) or out of attempts; re-queue with /jobs/retry
  skipped        a transcript quoting this voice note was already in the chat
  reply_unknown  wwebjs-api took the reply request but errored or timed out, so it may
                 already be sent; never retried automatically (no duplicate transcripts)

Stdlib only, so it runs on a stock python image from a bind mount or ConfigMap.
"""
import base64
import hmac
import json
import logging
import os
import random
import sys
import signal
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Also when run with `python -I` / -P, which leave the script's directory off sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantonese_ai_auth  # noqa: E402

WWEBJS_URL = os.environ.get("WWEBJS_URL", "http://wwebjs-api:3000")
# Shared secret with wwebjs-api: it sends it on every webhook, and the admin API requires it.
WWEBJS_API_KEY = os.environ.get("WWEBJS_API_KEY", "")
STT_URL = os.environ.get("STT_URL", "https://cantonese.ai/api/stt")
# cantonese.ai credential: an API key (wins) or the token saved by `python app.py login`.
CANTONESE_AI_API_KEY = os.environ.get("CANTONESE_AI_API_KEY", "")
CANTONESE_AI_ISSUER = os.environ.get("CANTONESE_AI_ISSUER", cantonese_ai_auth.DEFAULT_ISSUER)
CANTONESE_AI_OAUTH_CLIENT_ID = os.environ.get("CANTONESE_AI_OAUTH_CLIENT_ID", cantonese_ai_auth.DEFAULT_CLIENT_ID)
CANTONESE_AI_OAUTH_SCOPE = os.environ.get("CANTONESE_AI_OAUTH_SCOPE", cantonese_ai_auth.DEFAULT_SCOPE)
STT_LANG = os.environ.get("STT_LANG", "cantonese")
VOICE_TYPES = set(os.environ.get("VOICE_TYPES", "ptt,audio").split(","))
REPLY_PREFIX = os.environ.get("REPLY_PREFIX", "📝 ")
DB_PATH = os.environ.get("DB_PATH", "/data/queue.db")
CANTONESE_AI_TOKEN_PATH = os.environ.get("CANTONESE_AI_TOKEN_PATH",
                                         os.path.join(os.path.dirname(DB_PATH) or ".", "cantonese-ai-token.json"))
WORKERS = int(os.environ.get("WORKERS", "2"))
# Per-attempt STT timeout. Transient failures are rescheduled with full-jitter backoff: up to 2s, 4s, 8s, ... capped at BACKOFF_MAX_S,
# so the default 10 attempts span roughly an hour.
STT_TIMEOUT_S = float(os.environ.get("STT_TIMEOUT_S", "150"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "10"))
BACKOFF_BASE_S = float(os.environ.get("BACKOFF_BASE_S", "2"))
BACKOFF_MAX_S = float(os.environ.get("BACKOFF_MAX_S", "900"))
# Audio of finished jobs is dropped; rows are kept this long.
KEEP_DONE_DAYS = float(os.environ.get("KEEP_DONE_DAYS", "30"))
# Voice notes from others wait a random DEFER_MIN_S..DEFER_MAX_S first. The real guard against two
# agents in one chat both replying is the check for an existing transcript right before replying.
DEFER_MIN_S = float(os.environ.get("DEFER_MIN_S", "0"))
DEFER_MAX_S = float(os.environ.get("DEFER_MAX_S", "3"))
DEDUPE_SCAN_LIMIT = int(os.environ.get("DEDUPE_SCAN_LIMIT", "50"))
TRANSCRIPT_MARKER = REPLY_PREFIX.strip() or REPLY_PREFIX
REPLY_TIMEOUT_S = float(os.environ.get("REPLY_TIMEOUT_S", "60"))
# A 'processing' job is taken over only after its lease (one STT timeout + reply) runs out.
LEASE_S = float(os.environ.get("LEASE_S", STT_TIMEOUT_S + 90))
# Optional pairing / removal alert e-mails through SendGrid; off unless all three are set.
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_URL = os.environ.get("SENDGRID_URL", "https://api.sendgrid.com/v3/mail/send")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
SESSION_EVENTS = {"session_paired": "paired", "session_removed": "removed"}
# Never post faster than one transcript per SEND_MIN_INTERVAL_S per WhatsApp number (a burst of
# replies looks like spam and risks a ban). A job that would wait
# longer than SEND_MAX_WAIT_S is rescheduled instead of holding a worker.
SEND_MIN_INTERVAL_S = float(os.environ.get("SEND_MIN_INTERVAL_S", "5"))
SEND_MAX_WAIT_S = float(os.environ.get("SEND_MAX_WAIT_S", "30"))
# Catch-up (notes re-sent after a restart or re-link): only notes this recent get a transcript;
# older ones are recorded as skipped, never posted hours late. The chat is searched this deep
# for an existing transcript first (50 messages is not deep enough in busy chats).
CATCHUP_MAX_AGE_S = float(os.environ.get("CATCHUP_MAX_AGE_S", "3600"))
CATCHUP_SCAN_LIMIT = int(os.environ.get("CATCHUP_SCAN_LIMIT", "1000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-transcriber")


class PermanentError(Exception):
    """Retrying won't help (4xx other than 429)."""


class ReplyUnknown(Exception):
    """wwebjs-api received the reply request; it may or may not have been sent."""


class QuotedMessageMissing(Exception):
    """The session doesn't have the voice note (e.g. a freshly re-linked device without that
    history), so WhatsApp can't build the quote. Raised before anything is sent."""


class TransientError(Exception):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


# ---------------------------------------------------------------- queue

_db_lock = threading.Lock()
_db = None
_wake = threading.Event()
_stopping = threading.Event()
_active = [0]
_active_lock = threading.Lock()
OWNER = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        # Two processes may share this file (e.g. during a rolling update on one host).
        _db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=30)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, session_id TEXT, chat_id TEXT, mimetype TEXT, audio BLOB,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
            last_error TEXT, transcript TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        _db.execute("CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, next_at)")
        cols = {r[1] for r in _db.execute("PRAGMA table_info(jobs)")}
        for col, decl in (("owner", "TEXT"), ("lease_until", "REAL"), ("msg_key", "TEXT"), ("from_me", "INTEGER"),
                          ("check_existing", "INTEGER")):
            if col not in cols:
                _db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        # One job per voice note even when several of our sessions receive it.
        _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_msg_key ON jobs (msg_key)")
        _db.execute("CREATE TABLE IF NOT EXISTS send_slots (session_id TEXT PRIMARY KEY, next_at REAL NOT NULL)")
        _db.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT, payload TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
            last_error TEXT, owner TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    return _db


def q(sql, args=()):
    with _db_lock:
        return db().execute(sql, args).fetchall()


def enqueue(job_id, msg_key, from_me, session_id, chat_id, mimetype, audio, check_existing=False,
            skip_reason=None):
    now = time.time()
    status = "skipped" if skip_reason else "pending"
    if skip_reason:
        audio = None
    # The sender's own account transcribes at once; everyone else waits so the sender's
    # (or another receiver's) transcript can land first and be detected.
    next_at = now if from_me else now + random.uniform(DEFER_MIN_S, DEFER_MAX_S)
    with _db_lock:
        cur = db().execute(
            "INSERT OR IGNORE INTO jobs (id, msg_key, from_me, check_existing, session_id, chat_id, mimetype, audio,"
            " status, next_at, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, msg_key, int(from_me), int(check_existing), session_id, chat_id, mimetype, audio, status,
             next_at, skip_reason, now, now))
        if skip_reason:
            return bool(cur.rowcount)
        if not cur.rowcount and from_me:
            # Another of our sessions received this note first; the sender's session takes
            # the job over (no wait) if it hasn't started yet.
            cur = db().execute(
                "UPDATE jobs SET id = ?, from_me = 1, session_id = ?, chat_id = ?, next_at = ?, updated_at = ?"
                " WHERE msg_key = ? AND status = 'pending' AND from_me = 0 AND id != ?",
                (job_id, session_id, chat_id, now, now, msg_key, job_id))
    if cur.rowcount:
        _wake.set()
    return bool(cur.rowcount)


def claim(table="jobs"):
    """Atomically take the next due row of `table`, or a 'processing' one whose lease ran out
    (its process died mid-job). A live process's work is never taken over."""
    now = time.time()
    with _db_lock:
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE (status = 'pending' AND next_at <= ?)"
                " OR (status = 'processing' AND lease_until < ?) ORDER BY next_at LIMIT 1", (now, now)).fetchone()
            if row:
                conn.execute(f"UPDATE {table} SET status = 'processing', attempts = attempts + 1, owner = ?,"
                             " lease_until = ?, updated_at = ? WHERE id = ?", (OWNER, now + LEASE_S, now, row["id"]))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {**dict(row), "attempts": row["attempts"] + 1} if row else None


def finish(job_id, status, table="jobs", **fields):
    fields.update(status=status, updated_at=time.time())
    if status == "done" and table == "jobs":
        fields["audio"] = None
    fields.setdefault("lease_until", None)
    cols = ", ".join(f"{k} = ?" for k in fields)
    # Only if we still own it: after a lost lease another process may have the job.
    q(f"UPDATE {table} SET {cols} WHERE id = ? AND owner = ?", (*fields.values(), job_id, OWNER))


def reserve_send_slot(session_id):
    """Atomically book the next send time for this WhatsApp number (shared by every process).
    Returns seconds to wait, or None if that is longer than SEND_MAX_WAIT_S (nothing booked)."""
    now = time.time()
    with _db_lock:
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT next_at FROM send_slots WHERE session_id = ?", (session_id,)).fetchone()
            slot = max(now, row["next_at"] if row else 0.0)
            if slot - now > SEND_MAX_WAIT_S:
                conn.execute("COMMIT")
                return None
            conn.execute("INSERT INTO send_slots (session_id, next_at) VALUES (?, ?) ON CONFLICT(session_id)"
                         " DO UPDATE SET next_at = excluded.next_at", (session_id, slot + SEND_MIN_INTERVAL_S))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return slot - now


class SendBusy(Exception):
    """This number's send queue is longer than SEND_MAX_WAIT_S; try the job again later."""


def paced_send_reply(session_id, *args, **kwargs):
    wait = reserve_send_slot(session_id)
    if wait is None:
        raise SendBusy(session_id)
    if wait > 0:
        time.sleep(wait)
    return send_reply(session_id, *args, **kwargs)


def backoff_s(attempt, retry_after=None):
    delay = random.uniform(0, min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (attempt - 1)))
    try:
        delay = max(delay, min(BACKOFF_MAX_S, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return delay


# ---------------------------------------------------------------- http clients

def post_json(url, body, headers, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def post_multipart(url, fields, files, headers, timeout=300):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (filename, data, ctype) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{filename}"\r\n'
                     f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


_auth = None
_auth_lock = threading.Lock()


def stt_auth():
    global _auth
    with _auth_lock:
        if _auth is None:
            _auth = cantonese_ai_auth.CantoneseAiAuth(
                api_key=CANTONESE_AI_API_KEY, token_path=CANTONESE_AI_TOKEN_PATH,
                client_id=CANTONESE_AI_OAUTH_CLIENT_ID, issuer=CANTONESE_AI_ISSUER)
        return _auth


def stt_header(auth):
    try:
        return auth.authorization_header()
    except cantonese_ai_auth.AuthError as e:
        # Kept queued (not failed): signing in or setting a key later lets the job through.
        raise TransientError(f"cantonese.ai credential: {e}") from None


def transcribe(audio, mimetype, tag=""):
    ext = "ogg" if "ogg" in mimetype else (mimetype.split("/")[-1].split(";")[0] or "ogg")
    auth = stt_auth()

    def post(header):
        return post_multipart(
            STT_URL,
            {"lang": STT_LANG, "with_timestamp": "false", "with_diarization": "false", "wait_for_completion": "true"},
            {"data": (f"voice.{ext}", audio, mimetype)},
            {"authorization": header},
            timeout=STT_TIMEOUT_S,
        )

    def call():
        header = stt_header(auth)
        try:
            return post(header)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            # A signed-in access token may have expired early or been revoked: refresh once, retry.
            try:
                retry = auth.handle_unauthorized(header)
            except cantonese_ai_auth.AuthError as auth_error:
                raise TransientError(f"cantonese.ai credential: {auth_error}") from None
            if not retry:
                raise
            e.close()
            return post(stt_header(auth))

    try:
        body = call()
    except urllib.error.HTTPError as e:
        detail = f"stt {e.code}: {e.read()[:300]!r}"
        if 400 <= e.code < 500 and e.code != 429:
            raise PermanentError(detail) from e
        raise TransientError(detail, e.headers.get("retry-after")) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
        raise TransientError(f"stt {type(e).__name__}: {e}") from e
    data = body.get("data") or {}
    log.info("%s stt request_id=%s chars=%d", tag, data.get("request_id"), len(data.get("transcription") or ""))
    return data.get("transcription") or ""


def send_reply(session_id, chat_id, msg_id, text, quote=True):
    body = {"chatId": chat_id, "contentType": "string", "content": text}
    if quote:
        body["options"] = {"quotedMessageId": msg_id}
    try:
        return post_json(f"{WWEBJS_URL}/client/sendMessage/{session_id}", body, {"x-api-key": WWEBJS_API_KEY},
                         timeout=REPLY_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300]
        # wwebjs-api's session check answers 404 before anything is sent (session restarting,
        # or the number has to be linked again), so retrying cannot duplicate the transcript.
        if e.code == 404 and (b"session_not_connected" in detail or b"session_not_found" in detail):
            raise TransientError(f"wwebjs {e.code}: {detail!r}") from e
        # whatsapp-web.js failing to look up the quoted message, before sending.
        if quote and e.code == 500 and b"reading 'getChat'" in detail:
            raise QuotedMessageMissing(f"wwebjs {e.code}: {detail!r}") from e
        raise ReplyUnknown(f"wwebjs {e.code}: {detail!r}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise ReplyUnknown(f"wwebjs timeout: {e}") from e
        # Refused / no route: the request never reached wwebjs-api (e.g. container restarting).
        raise TransientError(f"wwebjs {e}") from e
    except (TimeoutError, ConnectionResetError) as e:
        raise ReplyUnknown(f"wwebjs {type(e).__name__}: {e}") from e


def already_transcribed(job):
    """True if the chat already has a transcript quoting this voice note, posted by any
    account running this service (another of our sessions or someone else's deployment)."""
    try:
        limit = CATCHUP_SCAN_LIMIT if job.get("check_existing") else DEDUPE_SCAN_LIMIT
        r = post_json(f"{WWEBJS_URL}/chat/fetchMessages/{job['session_id']}",
                      {"chatId": job["chat_id"], "searchOptions": {"limit": limit}},
                      {"x-api-key": WWEBJS_API_KEY}, timeout=REPLY_TIMEOUT_S)
    except Exception as e:
        log.warning("%s could not check chat for an existing transcript: %s", job["id"], e)
        return False  # a rare duplicate beats a missing transcript
    return any((m.get("_data") or {}).get("quotedStanzaID") == job["msg_key"]
               and (m.get("body") or "").startswith(TRANSCRIPT_MARKER)
               for m in r.get("messages") or [])


# ---------------------------------------------------------------- pairing / removal e-mails

def enqueue_notification(kind, session_id, data):
    """Queue a session_paired / session_removed e-mail. The eventId from wwebjs-api makes
    repeats (webhook re-sent, removal seen twice) collapse into one e-mail."""
    now = time.time()
    event_id = str(data.get("eventId") or f"{kind}:{session_id}:{now}")
    payload = {k: data.get(k) for k in ("phone", "pushname", "pairedAt", "reason")}
    # Without alert e-mails the event is only recorded (GET /notifications).
    status, note = ("pending", None) if alerts_enabled() else ("recorded", "alert e-mails not configured")
    with _db_lock:
        cur = db().execute(
            "INSERT OR IGNORE INTO notifications (id, kind, session_id, payload, status, next_at, last_error,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, kind, session_id, json.dumps(payload), status, now, note, now, now))
    if cur.rowcount:
        _wake.set()
        log.info("%s for session %s (%s)", f"queued {kind} e-mail" if status == "pending" else f"recorded {kind}",
                 session_id, payload.get("phone"))
    return event_id


def format_time(epoch_s):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(epoch_s))


def notification_email(kind, session_id, payload, created_at):
    phone = f"+{payload['phone']}" if payload.get("phone") else "(unknown number)"
    name = f" ({payload['pushname']})" if payload.get("pushname") else ""
    lines = [f"Session: {session_id}", f"Number: {phone}{name}"]
    if kind == "session_paired":
        subject = f"WhatsApp number paired: {phone}"
        lines.insert(0, f"{phone}{name} was linked to the WhatsApp voice-note transcriber.")
        lines.append(f"Paired at: {format_time((payload.get('pairedAt') or created_at * 1000) / 1000)}")
        lines.append("Voice notes sent and received on this number are now transcribed.")
    else:
        subject = f"WhatsApp number removed: {phone}"
        lines.insert(0, f"{phone}{name} was unlinked from the WhatsApp voice-note transcriber.")
        lines.append(f"Removed at: {format_time(created_at)}")
        lines.append(f"Reason: {payload.get('reason') or 'unknown'}")
        if payload.get("pairedAt"):
            lines.append(f"Had been paired since: {format_time(payload['pairedAt'] / 1000)}")
    lines.append("")
    lines.append("Sent by the whatsapp-agent voice-transcriber.")
    return subject, "\n".join(lines)


def alerts_enabled():
    return bool(SENDGRID_API_KEY and ALERT_EMAIL_TO and ALERT_EMAIL_FROM)


def send_email(subject, text):
    if not alerts_enabled():
        raise PermanentError("alert e-mails are not configured (SENDGRID_API_KEY, ALERT_EMAIL_TO, ALERT_EMAIL_FROM)")
    body = {"personalizations": [{"to": [{"email": ALERT_EMAIL_TO}]}], "from": {"email": ALERT_EMAIL_FROM},
            "subject": subject, "content": [{"type": "text/plain", "value": text}]}
    req = urllib.request.Request(SENDGRID_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"authorization": f"Bearer {SENDGRID_API_KEY}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REPLY_TIMEOUT_S) as r:
            r.read()
    except urllib.error.HTTPError as e:
        detail = f"sendgrid {e.code}: {e.read()[:300]!r}"
        if 400 <= e.code < 500 and e.code != 429:
            raise PermanentError(detail) from e
        raise TransientError(detail, e.headers.get("retry-after")) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # A timeout may still have been delivered; an occasional duplicate alert is acceptable.
        raise TransientError(f"sendgrid {type(e).__name__}: {e}") from e


def process_notification(item):
    item_id = item["id"]
    try:
        subject, text = notification_email(item["kind"], item["session_id"], json.loads(item["payload"]),
                                           item["created_at"])
        send_email(subject, text)
        finish(item_id, "sent", table="notifications", last_error=None)
        log.info("e-mailed alert: %s", subject)
    except PermanentError as e:
        finish(item_id, "failed", table="notifications", last_error=str(e))
        log.error("%s e-mail failed permanently: %s", item_id, e)
    except TransientError as e:
        if item["attempts"] >= MAX_ATTEMPTS:
            finish(item_id, "failed", table="notifications", last_error=f"{e} (gave up after {item['attempts']} attempts)")
            log.error("%s e-mail gave up: %s", item_id, e)
            return
        delay = backoff_s(item["attempts"], e.retry_after)
        finish(item_id, "pending", table="notifications", last_error=str(e), next_at=time.time() + delay)
        log.warning("%s e-mail attempt %d failed: %s; retrying in %.1fs", item_id, item["attempts"], e, delay)
    except Exception as e:
        finish(item_id, "failed", table="notifications", last_error=f"{type(e).__name__}: {e}")
        log.exception("%s e-mail crashed", item_id)


# ---------------------------------------------------------------- worker

def process(job):
    job_id = job["id"]
    # Others' notes may already have a transcript from another account; notes re-sent by the
    # wwebjs-api catch-up may already have ours.
    check = (not job["from_me"] or job.get("check_existing")) and job["msg_key"]
    try:
        if check and already_transcribed(job):
            finish(job_id, "skipped", last_error=None)
            log.info("%s already transcribed in chat, skipping", job_id)
            return
        text = job["transcript"]
        if text is None:
            text = transcribe(job["audio"], job["mimetype"] or "audio/ogg", tag=job_id).strip()
            # Saved so a reply retry doesn't transcribe again.
            q("UPDATE jobs SET transcript = ? WHERE id = ? AND owner = ?", (text, job_id, OWNER))
        if not text:
            finish(job_id, "done", last_error="empty transcript")
            log.info("empty transcript for %s", job_id)
            return
        if check and already_transcribed(job):  # may have landed while STT ran
            finish(job_id, "skipped", last_error=None)
            log.info("%s transcribed by someone else during STT, skipping", job_id)
            return
        try:
            paced_send_reply(job["session_id"], job["chat_id"], job_id, REPLY_PREFIX + text)
        except QuotedMessageMissing as e:
            log.warning("%s cannot be quoted (%s); sending the transcript without the quote", job_id, e)
            paced_send_reply(job["session_id"], job["chat_id"], job_id, f"{REPLY_PREFIX}(voice note) {text}",
                             quote=False)
        finish(job_id, "done", last_error=None)
        log.info("replied to %s with %d chars", job_id, len(text))
    except SendBusy:
        # Not an attempt: just come back when this number's send queue has room.
        q("UPDATE jobs SET attempts = attempts - 1 WHERE id = ? AND owner = ?", (job_id, OWNER))
        finish(job_id, "pending", next_at=time.time() + SEND_MAX_WAIT_S / 2)
    except PermanentError as e:
        finish(job_id, "failed", last_error=str(e))
        log.error("%s failed permanently: %s", job_id, e)
    except ReplyUnknown as e:
        finish(job_id, "reply_unknown", last_error=str(e))
        log.error("%s reply outcome unknown, not retrying: %s", job_id, e)
    except TransientError as e:
        if job["attempts"] >= MAX_ATTEMPTS:
            finish(job_id, "failed", last_error=f"{e} (gave up after {job['attempts']} attempts)")
            log.error("%s gave up after %d attempts: %s", job_id, job["attempts"], e)
            return
        delay = backoff_s(job["attempts"], e.retry_after)
        finish(job_id, "pending", last_error=str(e), next_at=time.time() + delay)
        log.warning("%s attempt %d failed: %s; retrying in %.1fs", job_id, job["attempts"], e, delay)
    except Exception as e:  # bug or corrupt row: park it rather than loop
        finish(job_id, "failed", last_error=f"{type(e).__name__}: {e}")
        log.exception("%s crashed", job_id)


def worker():
    while not _stopping.is_set():
        try:
            worker_step()
        except Exception:  # e.g. the database stayed locked; never let the thread die
            log.exception("worker error")
            time.sleep(1)


def worker_step():
    for table, handle in (("jobs", process), ("notifications", process_notification)):
        item = claim(table)
        if item:
            with _active_lock:
                _active[0] += 1
            try:
                handle(item)
            finally:
                with _active_lock:
                    _active[0] -= 1
            return
    # Poll at least every 2s: another process sharing the queue (rolling update) can add
    # jobs or let a lease expire without waking us.
    nxt = q("SELECT MIN(t) AS t FROM (SELECT CASE WHEN status = 'pending' THEN next_at ELSE lease_until END AS t"
            " FROM jobs WHERE status IN ('pending', 'processing') UNION ALL"
            " SELECT CASE WHEN status = 'pending' THEN next_at ELSE lease_until END"
            " FROM notifications WHERE status IN ('pending', 'processing'))")[0]["t"]
    _wake.wait(timeout=min(2.0, max(0.1, (nxt - time.time()) if nxt else 2.0)))
    _wake.clear()


def janitor():
    while True:
        q("DELETE FROM jobs WHERE status = 'done' AND updated_at < ?", (time.time() - KEEP_DONE_DAYS * 86400,))
        time.sleep(3600)


# ---------------------------------------------------------------- webhook + admin

def accept_media(session_id, data):
    """Queue a voice note. Returns the job id, or None if it isn't one we handle."""
    message = data.get("message") or {}
    media = data.get("messageMedia") or {}
    if message.get("type") not in VOICE_TYPES:
        return None
    # Our own voice notes are transcribed too, in the chat they were sent to.
    chat_id = message.get("to") if message.get("fromMe") else message.get("from")
    msg_id = (message.get("id") or {}).get("_serialized")
    # The bare message key is the same on every account's copy of the message.
    msg_key = (message.get("id") or {}).get("id") or msg_id
    if chat_id == "status@broadcast" or not msg_id or not chat_id or not media.get("data"):
        return None
    # Strict: the lenient decoder silently drops bad characters and can yield empty audio.
    audio = base64.b64decode(media["data"], validate=True)
    if not audio:
        raise ValueError("empty audio")
    sent_at = message.get("timestamp") or 0
    if data.get("catchUp") and time.time() - sent_at > CATCHUP_MAX_AGE_S:
        if enqueue(msg_id, msg_key, bool(message.get("fromMe")), session_id, chat_id, None, None,
                   check_existing=True, skip_reason="too old for catch-up; not posted late"):
            log.info("catch-up note %s is %.0f min old, recorded as skipped", msg_id, (time.time() - sent_at) / 60)
        return msg_id
    if enqueue(msg_id, msg_key, bool(message.get("fromMe")), session_id, chat_id,
               media.get("mimetype") or "audio/ogg", audio, check_existing=bool(data.get("catchUp"))):
        log.info("queued voice note %s in %s: %d bytes %s", msg_id, chat_id, len(audio), media.get("mimetype"))
    return msg_id


JOB_FIELDS = "id, msg_key, from_me, session_id, chat_id, status, attempts, last_error, transcript, created_at, updated_at, next_at"


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, obj=None):
        body = json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if WWEBJS_API_KEY and hmac.compare_digest(self.headers.get("x-api-key", "").encode(), WWEBJS_API_KEY.encode()):
            return True
        self.reply(401, {"error": "unauthorized"})
        return False

    def do_GET(self):
        url = urlparse(self.path)
        if url.path not in ("/jobs", "/notifications"):
            return self.reply(200, {"ok": True})
        if not self.authed():
            return
        args = parse_qs(url.query)
        try:
            limit = max(1, min(1000, int((args.get("limit") or ["50"])[0])))
        except ValueError:
            return self.reply(400, {"error": "limit must be an integer"})
        status = (args.get("status") or [None])[0]
        if url.path == "/notifications":
            rows = q("SELECT id, kind, session_id, payload, status, attempts, last_error, created_at, updated_at"
                     " FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,))
            return self.reply(200, {"notifications": [dict(r) for r in rows]})
        rows = q(f"SELECT {JOB_FIELDS} FROM jobs" + (" WHERE status = ?" if status else "")
                 + " ORDER BY created_at DESC LIMIT ?", ((status, limit) if status else (limit,)))
        counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        self.reply(200, {"counts": counts, "jobs": [dict(r) for r in rows]})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        if not self.authed():
            return
        try:
            event = json.loads(body or b"{}")
        except ValueError:
            return self.reply(400, {"error": "bad json"})
        if not isinstance(event, dict):
            return self.reply(400, {"error": "body must be a JSON object"})
        if urlparse(self.path).path == "/jobs/retry":
            return self.retry(event)
        if event.get("dataType") in SESSION_EVENTS:
            try:
                enqueue_notification(event["dataType"], event.get("sessionId"), event.get("data") or {})
            except Exception:
                log.exception("could not queue session event")
                return self.reply(500, {"error": "queue failed"})
            return self.reply(200, {"ok": True})
        # wwebjs-api webhook: queue before acknowledging, so an accepted voice note is durable.
        if event.get("dataType") == "media":
            try:
                accept_media(event.get("sessionId"), event.get("data") or {})
            except ValueError as e:  # bad base64 / empty audio (binascii.Error is a ValueError)
                log.error("rejected media event: %s", e)
                return self.reply(400, {"error": f"bad media: {e}"})
            except Exception:
                log.exception("could not queue media event")
                return self.reply(500, {"error": "queue failed"})
        self.reply(200, {"ok": True})

    def retry(self, req):
        now = time.time()
        # Only jobs that still have their audio, or already have a transcript, can be redone.
        cond = "status IN ('failed', 'reply_unknown') AND (audio IS NOT NULL OR transcript IS NOT NULL)"
        if not isinstance(req, dict):
            return self.reply(400, {"error": "body must be a JSON object"})
        if "ids" in req:
            ids = req["ids"]
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
                return self.reply(400, {"error": "ids must be a non-empty list of message ids"})
            sql = f"UPDATE jobs SET status = 'pending', attempts = 0, next_at = ?, updated_at = ? WHERE {cond} AND id IN ({','.join('?' * len(ids))})"
            args = (now, now, *ids)
        elif req.get("status") in ("failed", "reply_unknown"):
            sql = f"UPDATE jobs SET status = 'pending', attempts = 0, next_at = ?, updated_at = ? WHERE {cond} AND status = ?"
            args = (now, now, req["status"])
        else:
            return self.reply(400, {"error": 'give {"ids": [...]} or {"status": "failed"|"reply_unknown"}'})
        with _db_lock:
            n = db().execute(sql, args).rowcount
        _wake.set()
        log.info("re-queued %d jobs (%s)", n, req)
        self.reply(200, {"requeued": n})

    def log_message(self, *args):
        pass


def on_sigterm(*_):
    # Stop taking jobs and let the ones in flight finish (the stop grace period in
    # docker-compose.yml / the k8s manifest covers one STT timeout).
    log.info("SIGTERM: draining")
    _stopping.set()
    _wake.set()


def cli(argv):
    cmd = argv[0] if argv else "serve"
    if cmd == "login":
        no_browser = "--no-browser" in argv[1:]
        try:
            cantonese_ai_auth.login(token_path=CANTONESE_AI_TOKEN_PATH, client_id=CANTONESE_AI_OAUTH_CLIENT_ID,
                                    issuer=CANTONESE_AI_ISSUER, scope=CANTONESE_AI_OAUTH_SCOPE,
                                    open_browser=not no_browser, listen=not no_browser)
        except cantonese_ai_auth.AuthError as e:
            print(f"Sign-in failed: {e}", file=sys.stderr)
            return 1
        if CANTONESE_AI_API_KEY:
            print("Note: CANTONESE_AI_API_KEY is also set and takes precedence over this sign-in.")
        return 0
    if cmd == "logout":
        removed = cantonese_ai_auth.TokenStore(CANTONESE_AI_TOKEN_PATH).delete()
        print("Signed out." if removed else "Was not signed in.")
        return 0
    if cmd in ("auth-status", "whoami"):
        print(cantonese_ai_auth.status(CANTONESE_AI_TOKEN_PATH, CANTONESE_AI_API_KEY))
        return 0
    if cmd != "serve":
        print(__doc__, file=sys.stderr)
        return 2
    serve()
    return 0


def serve():
    if not WWEBJS_API_KEY:
        log.error("WWEBJS_API_KEY is not set (generate one with `openssl rand -hex 32`); refusing to start")
        sys.exit(1)
    log.info("cantonese.ai credential: %s", cantonese_ai_auth.status(CANTONESE_AI_TOKEN_PATH, CANTONESE_AI_API_KEY))
    if not alerts_enabled():
        log.info("alert e-mails off (set SENDGRID_API_KEY, ALERT_EMAIL_TO and ALERT_EMAIL_FROM to enable)")
    signal.signal(signal.SIGTERM, on_sigterm)
    pending = q("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending', 'processing')")[0]["n"]
    log.info("queue at %s: %d pending/processing on startup (owner %s)", DB_PATH, pending, OWNER)
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _stopping.wait()
    while True:
        with _active_lock:
            if not _active[0]:
                break
        time.sleep(0.2)
    log.info("drained, exiting")


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
