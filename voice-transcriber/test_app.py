"""Tests for app.py. Stdlib only:

    cd voice-transcriber && python3 -m unittest -v

Unit tests call app functions against fake STT / wwebjs-api HTTP servers; end-to-end
tests run app.py as real processes (restarts, kill -9, SIGTERM, two processes sharing the queue).
"""
import base64
import email.parser
import importlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app.py")
KEY = "test-key"
STT_KEY = "sk-test-not-a-real-key"

# The dev shell may route HTTP through a proxy; everything here is local.
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(predicate, timeout=15.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------- fake backends

class FakeBackends:
    """Fake cantonese.ai STT (/api/stt) and wwebjs-api (sendMessage, fetchMessages).

    The audio bytes are b"AUDIO-<tag>", so each STT call is attributed to a voice note.
    stt_plan[tag] / reply_plan[quoted id] are lists of responses consumed in order;
    when empty the call succeeds.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.stt_plan, self.reply_plan = {}, {}
        self.stt_calls, self.stt_requests = [], []
        self.replies, self.fetches = [], []
        self.chat_messages, self.fetch_status = [], 200
        self.after_stt = None  # hook run after each STT call (e.g. someone else replies)
        self.emails, self.mail_plan = [], []
        backends = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["content-length"]))
                if self.path == "/api/stt":
                    return backends.stt(self, body)
                if self.path.startswith("/client/sendMessage/"):
                    return backends.send(self, body)
                if self.path.startswith("/chat/fetchMessages/"):
                    return backends.fetch(self, body)
                if self.path == "/v3/mail/send":
                    return backends.mail(self, body)
                self.respond(404, {})

            def respond(self, code, obj, headers=None, raw=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(raw if raw is not None else json.dumps(obj).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client gave up (timeout tests)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def stt(self, h, body):
        msg = email.parser.BytesParser().parsebytes(
            b"Content-Type: " + h.headers["content-type"].encode() + b"\r\n\r\n" + body)
        fields, files = {}, {}
        for part in msg.get_payload():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                files[name] = (part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
            else:
                fields[name] = part.get_payload(decode=True).decode()
        audio = files.get("data", ("", "", b""))[2]
        tag = audio.decode(errors="replace").removeprefix("AUDIO-")
        with self.lock:
            self.stt_calls.append(tag)
            self.stt_requests.append({"fields": fields, "files": files, "auth": h.headers.get("authorization")})
            plan = self.stt_plan.get(tag) or []
            step = plan.pop(0) if plan else {}
        time.sleep(step.get("delay", 0))
        if "raw" in step:
            h.respond(step.get("code", 200), None, raw=step["raw"])
        elif step.get("code", 200) == 200:
            text = step.get("text", f"transcript {tag}")
            h.respond(200, step.get("body", {"success": True, "data": {"transcription": text, "request_id": "r"}}))
        else:
            h.respond(step["code"], {"message": "fake error"}, step.get("headers"))
        if self.after_stt:
            self.after_stt(tag)

    def send(self, h, body):
        req = json.loads(body)
        quoted = (req.get("options") or {}).get("quotedMessageId")
        content = req["content"]
        with self.lock:
            self.replies.append({"session": h.path.rsplit("/", 1)[1], "chat": req["chatId"], "t": time.time(),
                                 "quoted": quoted, "content": content, "key": h.headers.get("x-api-key")})
            plan = self.reply_plan.get(quoted) or []
            step = plan.pop(0) if plan else {}
            code = step.get("code", 200)
            # Only successful sends land in chat (so same_transcript_already_posted can
            # see a concurrent worker's post the way WhatsApp would).
            if code == 200 and quoted is not None:
                self.chat_messages = list(self.chat_messages) + [
                    {"body": content, "fromMe": True, "_data": {"quotedStanzaID": quoted}}]
        time.sleep(step.get("delay", 0))
        h.respond(code, step.get("body", {"success": code == 200}))

    def fetch(self, h, body):
        with self.lock:
            self.fetches.append(json.loads(body))
            messages = list(self.chat_messages)
        if self.fetch_status != 200:
            return h.respond(self.fetch_status, {"success": False})
        h.respond(200, {"success": True, "messages": messages})

    def mail(self, h, body):
        with self.lock:
            self.emails.append({"body": json.loads(body), "auth": h.headers.get("authorization")})
            step = self.mail_plan.pop(0) if self.mail_plan else {}
        h.respond(step.get("code", 202), {}, step.get("headers"))

    def replies_to(self, quoted):
        with self.lock:
            return [r for r in self.replies if r["quoted"] == quoted]


def voice_event(tag, *, from_me=False, session="demo", serialized=None, key=None, chat="a@c.us",
                me="me@c.us", msg_type="ptt", data=None, mimetype="audio/ogg; codecs=opus", catch_up=False,
                sent_at=None):
    """A wwebjs-api 'media' webhook body for a voice note."""
    msg_id = {"_serialized": serialized or f"m{tag}"}
    if key:
        msg_id["id"] = key
    message = {"type": msg_type, "fromMe": from_me, "id": msg_id, "timestamp": int(sent_at or time.time()),
               "from": me if from_me else chat, "to": chat if from_me else me}
    media = {"mimetype": mimetype,
             "data": data if data is not None else base64.b64encode(f"AUDIO-{tag}".encode()).decode()}
    body = {"dataType": "media", "sessionId": session, "data": {"message": message, "messageMedia": media}}
    if catch_up:
        body["data"]["catchUp"] = True
    return body


# ---------------------------------------------------------------- unit tests

class AppUnitTest(unittest.TestCase):
    """Imports app.py once and points it at fresh fakes / a fresh DB per test."""

    @classmethod
    def setUpClass(cls):
        os.environ.update(WWEBJS_API_KEY=KEY, CANTONESE_AI_API_KEY=STT_KEY, STT_URL="http://unused",
                          DB_PATH="/nonexistent/x")
        sys.path.insert(0, HERE)
        cls.app = importlib.import_module("app")
        cls.app.log.setLevel("CRITICAL")

    def setUp(self):
        app = self.app
        self.fake = FakeBackends()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.mkdtemp()
        app._db = None
        app.DB_PATH = os.path.join(self.tmp, "queue.db")
        app._auth, app.CANTONESE_AI_API_KEY = None, STT_KEY
        app.CANTONESE_AI_TOKEN_PATH = os.path.join(self.tmp, "cantonese-ai-token.json")
        app.ALERT_EMAIL_TO, app.ALERT_EMAIL_FROM = "alerts@example.com", "agent@example.com"
        app.STT_URL = self.fake.url + "/api/stt"
        app.WWEBJS_URL = self.fake.url
        app.STT_TIMEOUT_S, app.REPLY_TIMEOUT_S = 1.0, 1.0
        app.DEFER_MIN_S, app.DEFER_MAX_S = 15.0, 45.0
        app.BACKOFF_BASE_S, app.BACKOFF_MAX_S = 2.0, 900.0
        app.LEASE_S, app.MAX_ATTEMPTS = 240.0, 10
        app.SENDGRID_URL, app.SENDGRID_API_KEY = self.fake.url + "/v3/mail/send", "sendgrid-key"
        app.SEND_MIN_INTERVAL_S, app.SEND_MAX_WAIT_S = 5.0, 30.0
        app.CATCHUP_MAX_AGE_S, app.CATCHUP_SCAN_LIMIT = 3600.0, 1000
        self.addCleanup(lambda: app._db and app._db.close())

    def rows(self):
        return [dict(r) for r in self.app.q("SELECT * FROM jobs ORDER BY created_at")]

    def dead_url(self):
        return f"http://127.0.0.1:{free_port()}"


class TestBackoff(AppUnitTest):
    def test_grows_exponentially_within_bounds(self):
        for attempt, ceiling in ((1, 2), (2, 4), (3, 8), (6, 64)):
            delays = [self.app.backoff_s(attempt) for _ in range(300)]
            self.assertTrue(all(0 <= d <= ceiling for d in delays), (attempt, max(delays)))
            self.assertGreater(max(delays), ceiling * 0.5)  # jitter spreads over the range

    def test_capped(self):
        self.assertTrue(all(self.app.backoff_s(40) <= 900 for _ in range(100)))

    def test_retry_after_is_a_floor(self):
        self.assertGreaterEqual(self.app.backoff_s(1, "30"), 30)
        self.assertEqual(self.app.backoff_s(1, "99999"), 900)

    def test_bad_retry_after_ignored(self):
        for value in ("Wed, 21 Oct 2015 07:28:00 GMT", "", None, "abc"):
            self.assertLessEqual(self.app.backoff_s(1, value), 2)


class TestTranscribe(AppUnitTest):
    def test_success_sends_expected_request(self):
        self.assertEqual(self.app.transcribe(b"AUDIO-1", "audio/ogg; codecs=opus"), "transcript 1")
        req = self.fake.stt_requests[0]
        self.assertEqual(req["auth"], f"Bearer {STT_KEY}")
        self.assertEqual(req["fields"], {"lang": "cantonese", "with_timestamp": "false",
                                         "with_diarization": "false", "wait_for_completion": "true"})
        name, ctype, data = req["files"]["data"]
        self.assertEqual((name, ctype, data), ("voice.ogg", "audio/ogg", b"AUDIO-1"))

    def test_extension_from_mimetype(self):
        self.app.transcribe(b"AUDIO-2", "audio/mpeg")
        self.assertEqual(self.fake.stt_requests[0]["files"]["data"][0], "voice.mpeg")

    def test_binary_audio_round_trips(self):
        audio = bytes(range(256)) * 50
        self.app.transcribe(audio, "audio/ogg")
        self.assertEqual(self.fake.stt_requests[0]["files"]["data"][2], audio)

    def test_permanent_4xx(self):
        for code in (400, 401, 402, 403, 404, 413, 422):
            self.fake.stt_plan["3"] = [{"code": code}]
            with self.assertRaises(self.app.PermanentError, msg=code):
                self.app.transcribe(b"AUDIO-3", "audio/ogg")

    def test_429_is_transient_with_retry_after(self):
        self.fake.stt_plan["4"] = [{"code": 429, "headers": {"Retry-After": "7"}}]
        with self.assertRaises(self.app.TransientError) as ctx:
            self.app.transcribe(b"AUDIO-4", "audio/ogg")
        self.assertEqual(ctx.exception.retry_after, "7")

    def test_5xx_transient(self):
        for code in (500, 502, 503, 504):
            self.fake.stt_plan["5"] = [{"code": code}]
            with self.assertRaises(self.app.TransientError, msg=code):
                self.app.transcribe(b"AUDIO-5", "audio/ogg")

    def test_connection_refused_transient(self):
        self.app.STT_URL = self.dead_url() + "/api/stt"
        with self.assertRaises(self.app.TransientError):
            self.app.transcribe(b"AUDIO-6", "audio/ogg")

    def test_timeout_transient(self):
        self.app.STT_TIMEOUT_S = 0.3
        self.fake.stt_plan["7"] = [{"delay": 1.0}]
        with self.assertRaises(self.app.TransientError):
            self.app.transcribe(b"AUDIO-7", "audio/ogg")

    def test_non_json_200_transient(self):
        self.fake.stt_plan["8"] = [{"raw": b"<html>bad gateway</html>"}]
        with self.assertRaises(self.app.TransientError):
            self.app.transcribe(b"AUDIO-8", "audio/ogg")

    def test_missing_transcription_is_empty(self):
        self.fake.stt_plan["9"] = [{"body": {"success": True, "data": {}}}]
        self.assertEqual(self.app.transcribe(b"AUDIO-9", "audio/ogg"), "")
        self.fake.stt_plan["9"] = [{"body": {"success": True}}]
        self.assertEqual(self.app.transcribe(b"AUDIO-9", "audio/ogg"), "")


class TestSendReply(AppUnitTest):
    def test_quotes_the_voice_note(self):
        self.app.send_reply("demo", "c@c.us", "mid", "📝 hi")
        self.assertEqual([{k: v for k, v in r.items() if k != "t"} for r in self.fake.replies],
                         [{"session": "demo", "chat": "c@c.us", "quoted": "mid",
                                              "content": "📝 hi", "key": KEY}])

    def test_server_error_is_unknown_not_retryable(self):
        for code in (400, 404, 500, 503):
            self.fake.reply_plan["mid"] = [{"code": code}]
            with self.assertRaises(self.app.ReplyUnknown, msg=code):
                self.app.send_reply("demo", "c@c.us", "mid", "x")

    def test_session_not_connected_is_retryable(self):
        # wwebjs-api's session check rejects before sending, so a retry cannot duplicate.
        for error in ("session_not_connected", "session_not_found"):
            self.fake.reply_plan["mid"] = [{"code": 404, "body": {"success": False, "error": error}}]
            with self.assertRaises(self.app.TransientError, msg=error):
                self.app.send_reply("demo", "c@c.us", "mid", "x")
        self.fake.reply_plan["mid"] = [{"code": 404, "body": {"success": False, "error": "Message not found"}}]
        with self.assertRaises(self.app.ReplyUnknown):
            self.app.send_reply("demo", "c@c.us", "mid", "x")

    def test_missing_quoted_message_detected(self):
        self.fake.reply_plan["mid"] = [{"code": 500, "body": {
            "success": False, "error": "Cannot read properties of undefined (reading 'getChat')"}}]
        with self.assertRaises(self.app.QuotedMessageMissing):
            self.app.send_reply("demo", "c@c.us", "mid", "x")

    def test_unquoted_reply_has_no_options(self):
        self.app.send_reply("demo", "c@c.us", "mid", "x", quote=False)
        self.assertIsNone(self.fake.replies[0]["quoted"])

    def test_unreachable_is_transient(self):
        self.app.WWEBJS_URL = self.dead_url()
        with self.assertRaises(self.app.TransientError):
            self.app.send_reply("demo", "c@c.us", "mid", "x")

    def test_timeout_is_unknown(self):
        self.app.REPLY_TIMEOUT_S = 0.3
        self.fake.reply_plan["mid"] = [{"delay": 1.0}]
        with self.assertRaises(self.app.ReplyUnknown):
            self.app.send_reply("demo", "c@c.us", "mid", "x")


class TestAlreadyTranscribed(AppUnitTest):
    job = {"id": "false_c_K1", "session_id": "demo", "chat_id": "c@g.us", "msg_key": "K1"}

    def check(self, messages, status=200):
        self.fake.chat_messages, self.fake.fetch_status = messages, status
        return self.app.already_transcribed(self.job)

    def test_transcript_quoting_this_note(self):
        self.assertTrue(self.check([{"body": "hi"}, {"body": "📝 字", "_data": {"quotedStanzaID": "K1"}}]))
        self.assertEqual(self.fake.fetches[-1], {"chatId": "c@g.us", "searchOptions": {"limit": 50}})

    def test_human_reply_quoting_the_note_does_not_count(self):
        self.assertFalse(self.check([{"body": "haha", "_data": {"quotedStanzaID": "K1"}}]))

    def test_transcript_of_another_note_does_not_count(self):
        self.assertFalse(self.check([{"body": "📝 字", "_data": {"quotedStanzaID": "K2"}}]))

    def test_odd_messages_tolerated(self):
        self.assertFalse(self.check([{}, {"body": None, "_data": None}, {"_data": {}}]))

    def test_errors_mean_not_found(self):
        self.assertFalse(self.check([], status=500))
        self.app.WWEBJS_URL = self.dead_url()
        self.assertFalse(self.app.already_transcribed(self.job))


class TestSameTranscriptAlreadyPosted(AppUnitTest):
    job = {"id": "true_c_K1", "session_id": "demo", "chat_id": "c@g.us", "msg_key": "K1"}

    def check(self, messages, text="字", status=200):
        self.fake.chat_messages, self.fake.fetch_status = messages, status
        return self.app.same_transcript_already_posted(self.job, text)

    def test_same_quoted_text(self):
        self.assertTrue(self.check([{"body": "📝 字", "fromMe": True, "_data": {"quotedStanzaID": "K1"}}]))

    def test_same_text_whitespace_normalized(self):
        self.assertTrue(self.check(
            [{"body": "📝   字  " + chr(10), "fromMe": True, "_data": {"quotedStanzaID": "K1"}}],
            text="  字 "))


    def test_different_quoted_text_does_not_count(self):
        self.assertFalse(self.check([{"body": "📝 other", "fromMe": True, "_data": {"quotedStanzaID": "K1"}}]))

    def test_same_text_not_from_us_does_not_count(self):
        self.assertFalse(self.check([{"body": "📝 字", "fromMe": False, "_data": {"quotedStanzaID": "K1"}}]))

    def test_same_text_without_from_me_counts(self):
        # fetch payloads sometimes omit fromMe; still treat as a duplicate to avoid double-post
        self.assertTrue(self.check([{"body": "📝 字", "_data": {"quotedStanzaID": "K1"}}]))

    def test_wrong_quote_target(self):
        self.assertFalse(self.check([{"body": "📝 字", "fromMe": True, "_data": {"quotedStanzaID": "K2"}}]))

    def test_errors_mean_not_found(self):
        self.assertFalse(self.check([], status=500))
        self.app.WWEBJS_URL = self.dead_url()
        self.assertFalse(self.app.same_transcript_already_posted(self.job, "字"))


class TestAcceptMedia(AppUnitTest):
    def accept(self, **kw):
        event = voice_event(kw.pop("tag", "1"), **kw)
        return self.app.accept_media(event["sessionId"], event["data"])

    def test_ignores_non_voice_status_and_incomplete(self):
        self.assertIsNone(self.accept(msg_type="image"))
        self.assertIsNone(self.accept(msg_type="chat"))
        self.assertIsNone(self.accept(chat="status@broadcast"))
        self.assertIsNone(self.accept(data=""))
        self.assertIsNone(self.app.accept_media("demo", {}))
        self.assertIsNone(self.app.accept_media("demo", {"message": {"type": "ptt"}, "messageMedia": {"data": "QQ=="}}))
        self.assertEqual(self.rows(), [])

    def test_audio_type_accepted(self):
        self.assertEqual(self.accept(msg_type="audio"), "m1")

    def test_incoming_replies_to_sender_chat_and_is_deferred(self):
        t = time.time()
        self.accept(chat="friend@c.us", key="K1")
        [row] = self.rows()
        self.assertEqual((row["chat_id"], row["from_me"], row["msg_key"], row["status"]), ("friend@c.us", 0, "K1", "pending"))
        self.assertGreaterEqual(row["next_at"], t + 15)
        self.assertLessEqual(row["next_at"], time.time() + 45)
        self.assertEqual(row["audio"], b"AUDIO-1")

    def test_own_note_replies_in_target_chat_at_once(self):
        self.accept(from_me=True, chat="group@g.us", key="K1")
        [row] = self.rows()
        self.assertEqual((row["chat_id"], row["from_me"]), ("group@g.us", 1))
        self.assertLessEqual(row["next_at"], time.time())

    def test_msg_key_falls_back_to_serialized_id(self):
        self.accept(serialized="false_x_ABC")
        self.assertEqual(self.rows()[0]["msg_key"], "false_x_ABC")

    def test_duplicate_webhook_one_job(self):
        self.accept(key="K1")
        self.accept(key="K1")
        self.assertEqual(len(self.rows()), 1)

    def test_same_note_on_two_of_our_sessions_one_job(self):
        self.accept(from_me=True, session="a", serialized="true_b_K1", key="K1")
        self.accept(from_me=False, session="b", serialized="false_a_K1", key="K1")
        [row] = self.rows()
        self.assertEqual((row["id"], row["session_id"]), ("true_b_K1", "a"))

    def test_sender_takes_over_job_from_receiver(self):
        self.accept(from_me=False, session="b", serialized="false_a_K1", key="K1", chat="a@c.us")
        self.accept(from_me=True, session="a", serialized="true_b_K1", key="K1", chat="b@c.us")
        [row] = self.rows()
        self.assertEqual((row["id"], row["session_id"], row["chat_id"], row["from_me"]), ("true_b_K1", "a", "b@c.us", 1))
        self.assertLessEqual(row["next_at"], time.time())

    def test_no_takeover_once_started(self):
        self.accept(from_me=False, session="b", serialized="false_a_K1", key="K1")
        self.app.q("UPDATE jobs SET status = 'done'")
        self.accept(from_me=True, session="a", serialized="true_b_K1", key="K1")
        [row] = self.rows()
        self.assertEqual((row["id"], row["status"]), ("false_a_K1", "done"))

    def test_invalid_or_empty_audio_rejected(self):
        for data in ("!!!not base64", "QUJD!", "QUJ", "===="):
            with self.assertRaises(ValueError, msg=data):
                self.accept(data=data)
        self.assertEqual(self.rows(), [])


class TestQueue(AppUnitTest):
    def add(self, job_id, next_at=0.0, status="pending"):
        self.app.q("INSERT INTO jobs (id, msg_key, from_me, session_id, chat_id, mimetype, audio, status, next_at,"
                   " created_at, updated_at) VALUES (?, ?, 1, 's', 'c', 'audio/ogg', x'00', ?, ?, 0, 0)",
                   (job_id, job_id, status, next_at))

    def test_claim_due_only_and_leases(self):
        self.add("later", next_at=time.time() + 60)
        self.assertIsNone(self.app.claim())
        self.add("now")
        job = self.app.claim()
        self.assertEqual((job["id"], job["attempts"]), ("now", 1))
        row = {r["id"]: r for r in self.rows()}["now"]
        self.assertEqual((row["status"], row["owner"], row["attempts"]), ("processing", self.app.OWNER, 1))
        self.assertGreater(row["lease_until"], time.time() + 200)
        self.assertIsNone(self.app.claim())  # live lease is not taken

    def test_expired_lease_is_reclaimed(self):
        self.add("j")
        self.app.claim()
        self.app.q("UPDATE jobs SET lease_until = ?", (time.time() - 1,))
        self.assertEqual(self.app.claim()["attempts"], 2)

    def test_finish_ignored_after_losing_the_job(self):
        self.add("j")
        self.app.claim()
        self.app.q("UPDATE jobs SET owner = 'other-pod'")
        self.app.finish("j", "done")
        self.assertEqual(self.rows()[0]["status"], "processing")

    def test_done_drops_audio_failed_keeps_it(self):
        self.add("a"); self.add("b")
        self.app.claim(); self.app.claim()
        self.app.finish("a", "done")
        self.app.finish("b", "failed", last_error="x")
        rows = {r["id"]: r for r in self.rows()}
        self.assertIsNone(rows["a"]["audio"])
        self.assertEqual(rows["b"]["audio"], b"\x00")
        self.assertIsNone(rows["a"]["lease_until"])

    def test_concurrent_claims_never_share_a_job(self):
        for i in range(200):
            self.add(f"j{i}")
        got, lock = [], threading.Lock()

        def run():
            while True:
                job = self.app.claim()
                if not job:
                    return
                with lock:
                    got.append(job["id"])
        threads = [threading.Thread(target=run) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(got), sorted(f"j{i}" for i in range(200)))

    def test_old_schema_is_migrated(self):
        conn = sqlite3.connect(self.app.DB_PATH)
        conn.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, session_id TEXT, chat_id TEXT, mimetype TEXT,
            audio BLOB, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
            last_error TEXT, transcript TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("INSERT INTO jobs VALUES ('old', 's', 'c', 'audio/ogg', x'00', 'pending', 0, 0, NULL, NULL, 0, 0)")
        conn.commit(); conn.close()
        job = self.app.claim()
        self.assertEqual(job["id"], "old")
        self.assertIsNone(job["msg_key"])

    def test_worker_survives_errors(self):
        app = self.app
        calls = []
        real_claim = app.claim

        def flaky_claim(table="jobs"):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_claim(table)
        app.claim = flaky_claim
        app._stopping.clear()
        self.add("j")
        processed = []
        real_process = app.process
        app.process = lambda job: processed.append(job["id"])
        try:
            t = threading.Thread(target=app.worker, daemon=True)
            t.start()
            self.assertTrue(wait_until(lambda: processed == ["j"], timeout=5))
        finally:
            app._stopping.set(); app._wake.set(); t.join(5)
            app.claim, app.process = real_claim, real_process
            app._stopping.clear()


class TestPacingAndCatchUp(AppUnitTest):
    def test_send_slots_are_spaced_per_number(self):
        waits = [self.app.reserve_send_slot("a") for _ in range(3)]
        self.assertAlmostEqual(waits[0], 0, delta=0.2)
        self.assertAlmostEqual(waits[1], 5, delta=0.2)
        self.assertAlmostEqual(waits[2], 10, delta=0.2)
        self.assertAlmostEqual(self.app.reserve_send_slot("b"), 0, delta=0.2)  # other number unaffected

    def test_no_slot_when_wait_too_long(self):
        self.app.SEND_MAX_WAIT_S = 12
        for _ in range(3):
            self.app.reserve_send_slot("a")  # 0, 5, 10
        self.assertIsNone(self.app.reserve_send_slot("a"))  # would be 15
        self.assertIsNone(self.app.reserve_send_slot("a"))  # nothing was booked by the refusal

    def test_old_catch_up_note_recorded_as_skipped(self):
        event = voice_event("1", from_me=True, key="K1", catch_up=True, sent_at=time.time() - 4 * 3600)
        self.app.accept_media("demo", event["data"])
        [row] = self.rows()
        self.assertEqual((row["status"], row["audio"]), ("skipped", None))
        self.assertIn("too old", row["last_error"])
        self.assertIsNone(self.app.claim())

    def test_recent_catch_up_note_queued(self):
        event = voice_event("1", from_me=True, key="K1", catch_up=True, sent_at=time.time() - 600)
        self.app.accept_media("demo", event["data"])
        self.assertEqual(self.rows()[0]["status"], "pending")

    def test_old_live_note_still_processed(self):
        # Only catch-up re-sends are age-limited; a live webhook is always handled.
        event = voice_event("1", from_me=True, key="K1", sent_at=time.time() - 4 * 3600)
        self.app.accept_media("demo", event["data"])
        self.assertEqual(self.rows()[0]["status"], "pending")

    def test_catch_up_check_scans_deep(self):
        job = {"id": "j", "session_id": "demo", "chat_id": "c", "msg_key": "K1", "check_existing": 1}
        self.app.already_transcribed(job)
        self.assertEqual(self.fake.fetches[-1]["searchOptions"]["limit"], 1000)
        self.app.already_transcribed({**job, "check_existing": 0})
        self.assertEqual(self.fake.fetches[-1]["searchOptions"]["limit"], 50)


class TestNotifications(AppUnitTest):
    paired = {"phone": "85200000000", "pushname": "Demo", "pairedAt": 1791782400000}

    def test_paired_email(self):
        subject, text = self.app.notification_email("session_paired", "phone-85200000000", self.paired, 1791782401)
        self.assertEqual(subject, "WhatsApp number paired: +85200000000")
        self.assertIn("+85200000000 (Demo) was linked", text)
        self.assertIn("Session: phone-85200000000", text)
        self.assertIn("Paired at: 2026-10-12 05:20:00 UTC", text)

    def test_removed_email(self):
        subject, text = self.app.notification_email(
            "session_removed", "phone-85200000000", {**self.paired, "reason": "unlinked (LOGOUT)"}, 1791790000)
        self.assertEqual(subject, "WhatsApp number removed: +85200000000")
        self.assertIn("Reason: unlinked (LOGOUT)", text)
        self.assertIn("Had been paired since: 2026-10-12 05:20:00 UTC", text)

    def test_unknown_number_still_readable(self):
        subject, text = self.app.notification_email("session_removed", "s", {}, 0)
        self.assertEqual(subject, "WhatsApp number removed: (unknown number)")
        self.assertIn("Reason: unknown", text)

    def test_send_email_request(self):
        self.app.send_email("subject", "text")
        [mail] = self.fake.emails
        self.assertEqual(mail["auth"], "Bearer sendgrid-key")
        self.assertEqual(mail["body"]["personalizations"], [{"to": [{"email": "alerts@example.com"}]}])
        self.assertEqual(mail["body"]["from"], {"email": "agent@example.com"})
        self.assertEqual(mail["body"]["content"], [{"type": "text/plain", "value": "text"}])

    def test_send_email_errors(self):
        for code, error in ((400, self.app.PermanentError), (401, self.app.PermanentError),
                            (403, self.app.PermanentError), (429, self.app.TransientError),
                            (500, self.app.TransientError), (503, self.app.TransientError)):
            self.fake.mail_plan = [{"code": code}]
            with self.assertRaises(error, msg=code):
                self.app.send_email("s", "t")
        self.app.SENDGRID_URL = self.dead_url()
        with self.assertRaises(self.app.TransientError):
            self.app.send_email("s", "t")

    def test_missing_key_is_permanent(self):
        self.app.SENDGRID_API_KEY = ""
        with self.assertRaises(self.app.PermanentError):
            self.app.send_email("s", "t")
        self.assertEqual(self.fake.emails, [])

    def test_alerts_off_by_default_only_recorded(self):
        for missing in ("SENDGRID_API_KEY", "ALERT_EMAIL_TO", "ALERT_EMAIL_FROM"):
            saved = getattr(self.app, missing)
            setattr(self.app, missing, "")
            try:
                self.assertFalse(self.app.alerts_enabled())
                self.app.enqueue_notification("session_paired", "s", {"eventId": f"e-{missing}", **self.paired})
            finally:
                setattr(self.app, missing, saved)
        rows = self.app.q("SELECT status, last_error FROM notifications")
        self.assertEqual({(r["status"], r["last_error"]) for r in rows}, {("recorded", "alert e-mails not configured")})
        self.assertIsNone(self.app.claim("notifications"))
        self.assertEqual(self.fake.emails, [])

    def test_no_default_recipient(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ALERT_", "SENDGRID"))}
        out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.'); import app; print(repr((app.ALERT_EMAIL_TO, "
                              "app.ALERT_EMAIL_FROM, app.alerts_enabled())))"], cwd=HERE, env=env,
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "('', '', False)")

    def test_same_event_queued_once(self):
        event = {"eventId": "paired:s:1", **self.paired}
        self.app.enqueue_notification("session_paired", "s", event)
        self.app.enqueue_notification("session_paired", "s", event)
        self.assertEqual(len(self.app.q("SELECT * FROM notifications")), 1)


# ---------------------------------------------------------------- end-to-end

class EndToEnd(unittest.TestCase):
    """Runs app.py as processes sharing one queue file, against FakeBackends."""

    def setUp(self):
        self.fake = FakeBackends()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "queue.db")
        self.env = dict(os.environ, WWEBJS_API_KEY=KEY, CANTONESE_AI_API_KEY=STT_KEY, DB_PATH=self.db,
                        STT_URL=self.fake.url + "/api/stt", WWEBJS_URL=self.fake.url,
                        BACKOFF_BASE_S="0.05", BACKOFF_MAX_S="0.2", MAX_ATTEMPTS="3",
                        STT_TIMEOUT_S="3", REPLY_TIMEOUT_S="1", LEASE_S="1.5",
                        DEFER_MIN_S="0", DEFER_MAX_S="0", WORKERS="2",
                        SENDGRID_URL=self.fake.url + "/v3/mail/send", SENDGRID_API_KEY="sendgrid-key",
                        ALERT_EMAIL_TO="alerts@example.com", ALERT_EMAIL_FROM="agent@example.com",
                        SEND_MIN_INTERVAL_S="0")
        self.procs = []
        self.addCleanup(self.stop_all)
        self.port = self.start()

    def start(self, **env):
        port = free_port()
        log_path = os.path.join(self.tmp, f"app-{port}.log")
        with open(log_path, "w") as log:
            proc = subprocess.Popen([sys.executable, "-I", APP], env=dict(self.env, PORT=str(port), **env),
                                    stdout=log, stderr=log)
        self.procs.append(proc)
        if not wait_until(lambda: self.get(port, "/") is not None, timeout=10):
            with open(log_path) as f:
                self.fail(f.read())
        return port

    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()

    def request(self, port, path, body=None, key=KEY, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     method="POST" if data is not None else "GET",
                                     headers={"x-api-key": key, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except (urllib.error.URLError, ConnectionError):
            return None

    def get(self, port, path):
        return self.request(port, path)

    def send(self, event, port=None):
        return self.request(port or self.port, "/webhook", event)[0]

    def job(self, msg_id, port=None):
        status, body = self.request(port or self.port, "/jobs?limit=1000")
        return next((j for j in body["jobs"] if j["id"] == msg_id), None)

    def wait_status(self, msg_id, *statuses, timeout=15):
        found = wait_until(lambda: (j := self.job(msg_id)) and j["status"] in statuses and j, timeout=timeout)
        self.assertTrue(found, f"{msg_id}: {self.job(msg_id)}")
        return found

    # -- admin API and webhook input handling

    def test_auth_required(self):
        self.assertEqual(self.request(self.port, "/webhook", voice_event("1"), key="wrong")[0], 401)
        self.assertEqual(self.request(self.port, "/jobs", key="wrong")[0], 401)
        self.assertEqual(self.request(self.port, "/jobs/retry", {"status": "failed"}, key="")[0], 401)
        time.sleep(0.3)
        self.assertEqual(self.fake.stt_calls, [])

    def test_bad_input_rejected_and_server_keeps_working(self):
        p = self.port
        self.assertEqual(self.request(p, "/webhook", raw=b"{not json")[0], 400)
        self.assertEqual(self.request(p, "/webhook", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.request(p, "/webhook", voice_event("x", data="!!!"))[0], 400)
        self.assertEqual(self.request(p, "/webhook", voice_event("y", data="QUJD!"))[0], 400)
        self.assertIsNone(self.job("mx"))
        self.assertIsNone(self.job("my"))
        self.assertEqual(self.request(p, "/jobs?limit=abc")[0], 400)
        for body in ({}, {"ids": "m1"}, {"ids": []}, {"ids": [1]}, {"status": "done"}, [1]):
            self.assertEqual(self.request(p, "/jobs/retry", body)[0], 400, body)
        self.assertEqual(self.request(p, "/webhook", {"dataType": "message", "data": {}})[0], 200)
        self.assertEqual(self.send(voice_event("1")), 200)
        self.wait_status("m1", "done")

    # -- processing outcomes

    def test_happy_path(self):
        self.send(voice_event("1", chat="friend@c.us"))
        job = self.wait_status("m1", "done")
        self.assertEqual(job["transcript"], "transcript 1")
        [reply] = self.fake.replies
        self.assertEqual((reply["chat"], reply["quoted"], reply["content"]), ("friend@c.us", "m1", "📝 transcript 1"))

    def test_transient_errors_retried_then_done(self):
        self.fake.stt_plan["1"] = [{"code": 503}, {"code": 429, "headers": {"Retry-After": "0"}}]
        self.send(voice_event("1"))
        job = self.wait_status("m1", "done")
        self.assertEqual((job["attempts"], self.fake.stt_calls.count("1"), len(self.fake.replies)), (3, 3, 1))

    def test_out_of_attempts_failed_then_manual_retry(self):
        self.fake.stt_plan["1"] = [{"code": 503}] * 3
        self.send(voice_event("1"))
        job = self.wait_status("m1", "failed")
        self.assertIn("gave up after 3 attempts", job["last_error"])
        self.assertEqual(self.request(self.port, "/jobs/retry", {"status": "failed"})[1], {"requeued": 1})
        self.wait_status("m1", "done")
        self.assertEqual(len(self.fake.replies), 1)

    def test_permanent_error_not_retried_until_asked(self):
        self.fake.stt_plan["1"] = [{"code": 400}]
        self.send(voice_event("1"))
        self.wait_status("m1", "failed")
        time.sleep(0.5)
        self.assertEqual(self.fake.stt_calls.count("1"), 1)
        self.assertEqual(self.request(self.port, "/jobs/retry", {"ids": ["m1", "unknown"]})[1], {"requeued": 1})
        self.wait_status("m1", "done")

    def test_reply_unknown_never_resent_automatically(self):
        self.fake.reply_plan["m1"] = [{"code": 500}]
        self.send(voice_event("1"))
        self.wait_status("m1", "reply_unknown")
        time.sleep(1.0)
        self.assertEqual(len(self.fake.replies_to("m1")), 1)
        self.request(self.port, "/jobs/retry", {"status": "reply_unknown"})
        self.wait_status("m1", "done")
        self.assertEqual((len(self.fake.replies_to("m1")), self.fake.stt_calls.count("1")), (2, 1))

    def test_session_down_then_back_replies_once(self):
        down = {"code": 404, "body": {"success": False, "error": "session_not_connected"}}
        self.fake.reply_plan["m1"] = [down, down]
        self.send(voice_event("1"))
        job = self.wait_status("m1", "done")
        self.assertEqual((job["attempts"], self.fake.stt_calls.count("1")), (3, 1))
        self.assertEqual([r["quoted"] for r in self.fake.replies], ["m1"] * 3)  # 2 rejected, 1 sent

    def test_quote_missing_falls_back_to_unquoted_reply(self):
        self.fake.reply_plan["m1"] = [{"code": 500, "body": {
            "success": False, "error": "Cannot read properties of undefined (reading 'getChat')"}}]
        self.send(voice_event("1", chat="friend@c.us"))
        self.wait_status("m1", "done")
        self.assertEqual([(r["quoted"], r["content"]) for r in self.fake.replies],
                         [("m1", "📝 transcript 1"), (None, "📝 (voice note) transcript 1")])
        self.assertEqual(self.fake.replies[1]["chat"], "friend@c.us")

    def test_reply_timeout_is_reply_unknown(self):
        self.fake.reply_plan["m1"] = [{"delay": 2}]
        self.send(voice_event("1"))
        self.wait_status("m1", "reply_unknown")

    def test_wwebjs_down_then_back_keeps_transcript(self):
        self.stop_all()
        self.port = self.start(WWEBJS_URL=f"http://127.0.0.1:{free_port()}", MAX_ATTEMPTS="100")
        self.send(voice_event("1"))
        wait_until(lambda: (self.job("m1") or {}).get("attempts", 0) >= 3)
        self.assertEqual(self.job("m1")["transcript"], "transcript 1")
        self.stop_all()
        self.port = self.start()
        self.wait_status("m1", "done")
        self.assertEqual((self.fake.stt_calls.count("1"), len(self.fake.replies)), (1, 1))

    def test_empty_transcript_no_reply(self):
        self.fake.stt_plan["1"] = [{"text": "   "}]
        self.send(voice_event("1"))
        job = self.wait_status("m1", "done")
        self.assertEqual(job["last_error"], "empty transcript")
        self.assertEqual(self.fake.replies, [])

    def test_malformed_stt_response_parks_job_and_others_continue(self):
        self.fake.stt_plan["1"] = [{"body": {"data": "not an object"}}]
        self.send(voice_event("1"))
        self.send(voice_event("2"))
        self.assertIn("AttributeError", self.wait_status("m1", "failed")["last_error"])
        self.wait_status("m2", "done")

    def test_many_notes_each_replied_once(self):
        for i in range(40):
            self.send(voice_event(str(i)))
        wait_until(lambda: len(self.fake.replies) >= 40, timeout=30)
        time.sleep(0.5)
        self.assertEqual(sorted(r["quoted"] for r in self.fake.replies), sorted(f"m{i}" for i in range(40)))

    # -- duplicates

    def test_note_from_others_waits_before_transcribing(self):
        self.stop_all()
        self.port = self.start(DEFER_MIN_S="1.5", DEFER_MAX_S="1.5")
        self.send(voice_event("1"))
        time.sleep(0.8)
        self.assertEqual(self.fake.stt_calls, [])
        self.wait_status("m1", "done")
        self.send(voice_event("2", from_me=True))  # own note: no wait
        wait_until(lambda: "2" in self.fake.stt_calls, timeout=1.0)
        self.assertIn("2", self.fake.stt_calls)

    def test_existing_transcript_in_chat_skips(self):
        self.fake.chat_messages = [{"body": "📝 someone else's", "_data": {"quotedStanzaID": "K1"}}]
        self.send(voice_event("1", serialized="false_g_K1_p", key="K1", chat="g@g.us"))
        self.wait_status("false_g_K1_p", "skipped")
        self.assertEqual((self.fake.stt_calls, self.fake.replies), ([], []))

    def test_transcript_landing_during_stt_skips_reply(self):
        def someone_else_replies(tag):
            self.fake.chat_messages = [{"body": "📝 theirs", "_data": {"quotedStanzaID": "K1"}}]
        self.fake.after_stt = someone_else_replies
        self.send(voice_event("1", serialized="false_g_K1_p", key="K1", chat="g@g.us"))
        self.wait_status("false_g_K1_p", "skipped")
        self.assertEqual((self.fake.stt_calls, self.fake.replies), (["1"], []))

    def test_check_failure_still_transcribes(self):
        self.fake.fetch_status = 500
        self.send(voice_event("1", key="K1"))
        self.wait_status("m1", "done")
        self.assertEqual(len(self.fake.replies), 1)

    def test_catch_up_own_note_already_transcribed_is_skipped(self):
        self.fake.chat_messages = [{"body": "📝 earlier", "_data": {"quotedStanzaID": "K1"}}]
        self.send(voice_event("1", from_me=True, key="K1", catch_up=True))
        self.wait_status("m1", "skipped")
        self.assertEqual((self.fake.stt_calls, self.fake.replies), ([], []))

    def test_catch_up_own_note_without_transcript_is_done(self):
        self.fake.chat_messages = [{"body": "📝 other", "_data": {"quotedStanzaID": "OTHER"}}]
        self.send(voice_event("1", from_me=True, key="K1", catch_up=True))
        self.wait_status("m1", "done")
        self.assertEqual((self.fake.stt_calls, len(self.fake.replies)), (["1"], 1))
        self.assertEqual(len(self.fake.fetches), 3)  # already_transcribed x2 + same-text check before send

    def test_own_note_skips_early_dedupe_but_checks_same_text_before_send(self):
        # Own notes skip already_transcribed early; still STT, then same-text check before reply.
        # Different body so we still post.
        self.fake.chat_messages = [{"body": "📝 x", "fromMe": True, "_data": {"quotedStanzaID": "K1"}}]
        self.send(voice_event("1", from_me=True, key="K1"))
        self.wait_status("m1", "done")
        self.assertEqual(self.fake.stt_calls, ["1"])
        self.assertEqual(len(self.fake.replies), 1)
        self.assertEqual(len(self.fake.fetches), 1)

    def test_same_transcript_quote_already_posted_skips_send(self):
        # from_me: no early already_transcribed; after STT the same reply text is already in chat.
        self.fake.stt_plan["1"] = [{"text": "hello"}]
        self.fake.chat_messages = [{"body": "📝 hello", "fromMe": True, "_data": {"quotedStanzaID": "K1"}}]
        self.send(voice_event("1", from_me=True, key="K1"))
        self.wait_status("m1", "skipped")
        self.assertEqual(self.fake.stt_calls, ["1"])
        self.assertEqual(self.fake.replies, [])

    def test_different_transcript_quote_still_posts(self):
        self.fake.stt_plan["1"] = [{"text": "hello"}]
        self.fake.chat_messages = [{"body": "📝 earlier different", "fromMe": True,
                                   "_data": {"quotedStanzaID": "K1"}}]
        self.send(voice_event("1", from_me=True, key="K1"))
        self.wait_status("m1", "done")
        self.assertEqual(len(self.fake.replies), 1)
        self.assertEqual(self.fake.replies[0]["content"], "📝 hello")

    def test_no_quote_still_posts(self):
        self.fake.stt_plan["1"] = [{"text": "hello"}]
        self.fake.chat_messages = []
        self.send(voice_event("1", from_me=True, key="K1"))
        self.wait_status("m1", "done")
        self.assertEqual(len(self.fake.replies), 1)
        self.assertEqual(self.fake.replies[0]["content"], "📝 hello")

    def test_two_of_our_numbers_in_one_chat_one_transcript(self):
        # Receivers wait (as with the default DEFER_*), so the sender's copy can take over.
        self.stop_all()
        self.port = self.start(DEFER_MIN_S="1", DEFER_MAX_S="1")
        # Direct chat between two linked numbers, in both arrival orders.
        self.send(voice_event("1", from_me=True, session="a", serialized="true_b_K1", key="K1"))
        self.send(voice_event("1", session="b", serialized="false_a_K1", key="K1"))
        self.send(voice_event("2", session="b", serialized="false_a_K2", key="K2"))
        self.send(voice_event("2", from_me=True, session="a", serialized="true_b_K2", key="K2"))
        # Group with both numbers, someone else speaking: same serialized id on both.
        self.send(voice_event("3", session="a", serialized="false_g_K3_p", key="K3"))
        self.send(voice_event("3", session="b", serialized="false_g_K3_p", key="K3"))
        for msg_id in ("true_b_K1", "true_b_K2", "false_g_K3_p"):
            self.wait_status(msg_id, "done")
        time.sleep(0.5)
        self.assertEqual(sorted((r["session"], r["quoted"]) for r in self.fake.replies),
                         [("a", "false_g_K3_p"), ("a", "true_b_K1"), ("a", "true_b_K2")])
        self.assertEqual(sorted(self.fake.stt_calls), ["1", "2", "3"])

    # -- restarts

    def test_sigterm_finishes_in_flight_job(self):
        self.fake.stt_plan["1"] = [{"delay": 1.5}]
        self.send(voice_event("1"))
        wait_until(lambda: "1" in self.fake.stt_calls)
        proc = self.procs[-1]
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(len(self.fake.replies), 1)
        self.port = self.start()
        self.assertEqual(self.job("m1")["status"], "done")

    def test_kill_mid_job_resumes_after_lease(self):
        self.fake.stt_plan["1"] = [{"delay": 3}]
        self.send(voice_event("1"))
        wait_until(lambda: "1" in self.fake.stt_calls)
        self.procs[-1].kill()
        self.procs[-1].wait()
        self.port = self.start()
        job = self.wait_status("m1", "done")
        self.assertEqual(job["attempts"], 2)
        self.assertEqual(len(self.fake.replies), 1)

    def test_rolling_update_two_processes_share_queue(self):
        for i in range(30):
            self.fake.stt_plan[str(i)] = [{"delay": 0.1}]
        old = self.port
        for i in range(15):
            self.send(voice_event(str(i)), port=old)
        new = self.start()  # new process comes up while the old one works
        for i in range(15, 30):
            self.send(voice_event(str(i)), port=new if i % 2 else old)
        self.procs[0].send_signal(signal.SIGTERM)  # old process drains
        self.assertEqual(self.procs[0].wait(timeout=20), 0)
        self.port = new
        wait_until(lambda: len(self.fake.replies) >= 30, timeout=30)
        time.sleep(1)
        self.assertEqual(sorted(r["quoted"] for r in self.fake.replies), sorted(f"m{i}" for i in range(30)))
        self.assertEqual(sorted(self.fake.stt_calls, key=int), [str(i) for i in range(30)])

    def test_janitor_drops_old_done_jobs_only(self):
        self.send(voice_event("1"))
        self.send(voice_event("2"))
        self.wait_status("m1", "done")
        self.wait_status("m2", "done")
        self.stop_all()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE jobs SET updated_at = 0 WHERE id = 'm1'")
        conn.execute("UPDATE jobs SET updated_at = 0, status = 'failed' WHERE id = 'm2'")
        conn.commit(); conn.close()
        self.port = self.start()
        wait_until(lambda: self.job("m1") is None, timeout=5)
        self.assertIsNone(self.job("m1"))
        self.assertEqual(self.job("m2")["status"], "failed")


    # -- pacing

    def test_replies_on_one_number_are_spaced(self):
        self.stop_all()
        self.port = self.start(SEND_MIN_INTERVAL_S="0.5")
        for i in range(5):
            self.send(voice_event(str(i), from_me=True))
        self.assertTrue(wait_until(lambda: len(self.fake.replies) >= 5, timeout=20))
        times = sorted(r["t"] for r in self.fake.replies)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.45 for g in gaps), gaps)

    def test_long_send_queue_drains_without_duplicates(self):
        self.stop_all()
        self.port = self.start(SEND_MIN_INTERVAL_S="0.4", SEND_MAX_WAIT_S="0.5")
        for i in range(8):
            self.send(voice_event(str(i), from_me=True))
        self.assertTrue(wait_until(lambda: len(self.fake.replies) >= 8, timeout=40))
        time.sleep(1)
        self.assertEqual(sorted(r["quoted"] for r in self.fake.replies), sorted(f"m{i}" for i in range(8)))
        self.assertEqual(sorted(self.fake.stt_calls, key=int), [str(i) for i in range(8)])
        self.assertTrue(all(self.job(f"m{i}")["attempts"] <= 2 for i in range(8)))

    def test_old_catch_up_notes_never_posted(self):
        self.send(voice_event("1", from_me=True, key="K1", catch_up=True, sent_at=time.time() - 3 * 3600))
        self.send(voice_event("2", from_me=True, key="K2", catch_up=True, sent_at=time.time() - 60))
        self.wait_status("m2", "done")
        self.assertEqual(self.job("m1")["status"], "skipped")
        self.assertEqual(([r["quoted"] for r in self.fake.replies], self.fake.stt_calls), (["m2"], ["2"]))

    # -- pairing / removal e-mails

    def notification(self, event_id):
        status, body = self.request(self.port, "/notifications?limit=100")
        return next((n for n in body["notifications"] if n["id"] == event_id), None)

    def wait_notification(self, event_id, status, timeout=15):
        found = wait_until(lambda: (n := self.notification(event_id)) and n["status"] == status and n, timeout=timeout)
        self.assertTrue(found, self.notification(event_id))
        return found

    def session_event(self, kind, event_id, **data):
        return self.send({"dataType": kind, "sessionId": "phone-85200000000",
                          "data": {"eventId": event_id, "phone": "85200000000", "pairedAt": 1791782400000, **data}})

    def test_paired_and_removed_emails(self):
        self.assertEqual(self.session_event("session_paired", "paired:p:1"), 200)
        self.wait_notification("paired:p:1", "sent")
        self.assertEqual(self.session_event("session_removed", "removed:p:1", reason="unlinked (LOGOUT)"), 200)
        self.wait_notification("removed:p:1", "sent")
        subjects = [m["body"]["subject"] for m in self.fake.emails]
        self.assertEqual(subjects, ["WhatsApp number paired: +85200000000", "WhatsApp number removed: +85200000000"])
        self.assertEqual(self.fake.emails[0]["body"]["personalizations"][0]["to"][0]["email"], "alerts@example.com")

    def test_repeated_event_one_email(self):
        for _ in range(3):
            self.session_event("session_removed", "removed:p:2", reason="session terminated via the API")
        self.wait_notification("removed:p:2", "sent")
        time.sleep(0.5)
        self.assertEqual(len(self.fake.emails), 1)

    def test_email_retried_on_sendgrid_errors(self):
        self.fake.mail_plan = [{"code": 500}, {"code": 429}]
        self.session_event("session_paired", "paired:p:3")
        n = self.wait_notification("paired:p:3", "sent")
        self.assertEqual((n["attempts"], len(self.fake.emails)), (3, 3))

    def test_email_rejected_is_failed_not_retried(self):
        self.fake.mail_plan = [{"code": 400}]
        self.session_event("session_paired", "paired:p:4")
        self.assertIn("sendgrid 400", self.wait_notification("paired:p:4", "failed")["last_error"])
        time.sleep(0.5)
        self.assertEqual(len(self.fake.emails), 1)

    def test_email_survives_restart(self):
        self.stop_all()
        self.port = self.start(SENDGRID_URL=f"http://127.0.0.1:{free_port()}/v3/mail/send", MAX_ATTEMPTS="100")
        self.session_event("session_paired", "paired:p:5")
        wait_until(lambda: (self.notification("paired:p:5") or {}).get("attempts", 0) >= 2)
        self.stop_all()
        self.port = self.start()
        self.wait_notification("paired:p:5", "sent")
        self.assertEqual(len(self.fake.emails), 1)

    def test_refuses_to_start_without_wwebjs_key(self):
        env = dict(self.env, WWEBJS_API_KEY="", PORT=str(free_port()))
        proc = subprocess.run([sys.executable, "-I", APP], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("WWEBJS_API_KEY is not set", proc.stderr)

    def test_stt_uses_bearer_api_key(self):
        self.send(voice_event("1", from_me=True))
        self.wait_status("m1", "done")
        self.assertEqual(self.fake.stt_requests[0]["auth"], f"Bearer {STT_KEY}")

    def test_notifications_need_the_key(self):
        self.assertEqual(self.request(self.port, "/notifications", key="wrong")[0], 401)
        self.assertEqual(self.request(self.port, "/webhook", {"dataType": "session_paired", "data": {}},
                                      key="wrong")[0], 401)
        time.sleep(0.3)
        self.assertEqual(self.fake.emails, [])

    def test_voice_notes_and_emails_together(self):
        for i in range(10):
            self.send(voice_event(str(i)))
            self.session_event("session_paired", f"paired:mix:{i}")
        wait_until(lambda: len(self.fake.replies) >= 10 and len(self.fake.emails) >= 10, timeout=20)
        time.sleep(0.5)
        self.assertEqual((len(self.fake.replies), len(self.fake.emails)), (10, 10))


if __name__ == "__main__":
    unittest.main()
