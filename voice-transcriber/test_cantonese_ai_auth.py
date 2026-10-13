"""Tests for cantonese_ai_auth.py and the signed-in STT path of app.py. Stdlib only:

    cd voice-transcriber && python3 -m unittest -v test_cantonese_ai_auth

A fake authorization server (metadata, token endpoint with PKCE checks and refresh-token
rotation) and a fake /api/stt that only accepts the current access token.
"""
import base64
import hashlib
import importlib
import io
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cantonese_ai_auth as auth  # noqa: E402

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

CLIENT_ID = "whatsapp-agent"
logging.getLogger("cantonese-ai-auth").setLevel(logging.CRITICAL)


class FakeAuthServer:
    def __init__(self, metadata=True):
        self.lock = threading.Lock()
        self.metadata = metadata
        self.codes = {}  # code -> (challenge, redirect_uri)
        self.access, self.refresh = set(), set()
        self.token_requests, self.stt_auth = [], []
        self.counter = 0
        self.expires_in = 3600
        self.reject_all_stt = False
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/.well-known/oauth-authorization-server" and srv.metadata:
                    return self.respond(200, {"issuer": srv.url, "authorization_endpoint": srv.url + "/custom/authorize",
                                              "token_endpoint": srv.url + "/custom/token"})
                self.respond(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.path in ("/custom/token", "/api/oauth/token"):
                    return srv.token(self, dict(urllib.parse.parse_qsl(body.decode())))
                if self.path == "/api/stt":
                    return srv.stt(self)
                self.respond(404, {})

            def respond(self, code, obj):
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(obj).encode())

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def issue(self, h):
        with self.lock:
            self.counter += 1
            access, refresh = f"at-{self.counter}", f"rt-{self.counter}"
            self.access.add(access)
            self.refresh.add(refresh)
        h.respond(200, {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                        "expires_in": self.expires_in, "scope": "user:inference"})

    def token(self, h, form):
        with self.lock:
            self.token_requests.append(form)
        if "client_secret" in form or form.get("client_id") != CLIENT_ID:
            return h.respond(401, {"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            entry = self.codes.pop(form.get("code"), None)
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if not entry or entry != (challenge, form.get("redirect_uri")):
                return h.respond(400, {"error": "invalid_grant", "error_description": "bad code or verifier"})
            return self.issue(h)
        if form.get("grant_type") == "refresh_token":
            with self.lock:
                ok = form.get("refresh_token") in self.refresh
                self.refresh.discard(form.get("refresh_token"))  # rotation: single use
            if not ok:
                return h.respond(400, {"error": "invalid_grant", "error_description": "refresh token used or revoked"})
            return self.issue(h)
        h.respond(400, {"error": "unsupported_grant_type"})

    def stt(self, h):
        header = h.headers.get("authorization") or ""
        with self.lock:
            self.stt_auth.append(header)
        token = header.removeprefix("Bearer ")
        if self.reject_all_stt or token not in self.access:
            return h.respond(401, {"message": "Unauthorized"})
        h.respond(200, {"success": True, "data": {"transcription": "你好", "request_id": "r"}})

    def approve(self, authorize_url):
        """Plays the user's browser: approve and return the redirect URL."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(authorize_url).query))
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        assert q["client_id"] == CLIENT_ID and q["scope"] == "user:inference"
        code = f"code-{len(self.codes)}-{time.time()}"
        self.codes[code] = (q["code_challenge"], q["redirect_uri"])
        return f"{q['redirect_uri']}?{urllib.parse.urlencode({'code': code, 'state': q['state']})}"


def tokens(access="at-x", refresh="rt-x", expires_in=3600):
    return {"access_token": access, "refresh_token": refresh, "expires_at": time.time() + expires_in}


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAuthServer()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "data", "cantonese-ai-token.json")

    def make_auth(self, **kw):
        return auth.CantoneseAiAuth(token_path=self.path, client_id=CLIENT_ID, issuer=self.fake.url, **kw)


class TestPkceAndUrls(AuthTestCase):
    def test_rfc7636_test_vector(self):
        self.assertEqual(auth.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_new_pair_is_valid_and_random(self):
        v1, c1 = auth.new_pkce_pair()
        v2, _ = auth.new_pkce_pair()
        self.assertNotEqual(v1, v2)
        self.assertTrue(43 <= len(v1) <= 128)
        self.assertRegex(v1, r"^[A-Za-z0-9\-._~]+$")
        self.assertEqual(c1, auth.code_challenge(v1))
        self.assertNotIn("=", c1)

    def test_authorize_url(self):
        url = auth.authorize_url("https://example.test/authorize", CLIENT_ID, "http://127.0.0.1:5555/callback",
                                 "user:inference", "st", "ch")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(q, {"response_type": "code", "client_id": CLIENT_ID, "scope": "user:inference",
                             "redirect_uri": "http://127.0.0.1:5555/callback", "state": "st",
                             "code_challenge": "ch", "code_challenge_method": "S256"})
        self.assertNotIn("client_secret", url)

    def test_discovery_uses_metadata(self):
        ep = auth.discover(self.fake.url)
        self.assertEqual(ep["token_endpoint"], self.fake.url + "/custom/token")

    def test_discovery_falls_back(self):
        self.fake.metadata = False
        ep = auth.discover(self.fake.url)
        self.assertEqual(ep, {"authorization_endpoint": self.fake.url + "/api/oauth/authorize",
                              "token_endpoint": self.fake.url + "/api/oauth/token"})
        dead = auth.discover("http://127.0.0.1:1")
        self.assertEqual(dead["token_endpoint"], "http://127.0.0.1:1/api/oauth/token")


class TestStateCheck(unittest.TestCase):
    def test_accepts_url_path_or_query(self):
        for value in ("http://127.0.0.1:5/callback?code=C&state=S", "/callback?code=C&state=S", "?code=C&state=S",
                      "code=C&state=S", "  http://127.0.0.1:5/callback?state=S&code=C  "):
            self.assertEqual(auth.parse_callback(value, "S"), "C", value)

    def test_state_mismatch_or_missing(self):
        for value in ("http://127.0.0.1:5/callback?code=C&state=EVIL", "http://127.0.0.1:5/callback?code=C", ""):
            with self.assertRaises(auth.StateMismatch, msg=value):
                auth.parse_callback(value, "S")

    def test_error_and_missing_code(self):
        with self.assertRaises(auth.AuthError) as ctx:
            auth.parse_callback("/callback?error=access_denied&state=S", "S")
        self.assertIn("access_denied", str(ctx.exception))
        with self.assertRaises(auth.AuthError):
            auth.parse_callback("/callback?state=S", "S")


class TestLogin(AuthTestCase):
    def test_paste_flow_saves_tokens_without_client_secret(self):
        out = io.StringIO()

        def paste(prompt):
            url = out.getvalue().split("\n\n  ")[1].split("\n")[0]
            return self.fake.approve(url)
        saved = auth.login(token_path=self.path, client_id=CLIENT_ID, issuer=self.fake.url, open_browser=False,
                           listen=False, input_fn=paste, out=out, timeout_s=10)
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("at-1", "rt-1"))
        self.assertEqual(auth.TokenStore(self.path).load()["access_token"], "at-1")
        [req] = self.fake.token_requests
        self.assertEqual(req["grant_type"], "authorization_code")
        self.assertNotIn("client_secret", req)
        self.assertTrue(req["redirect_uri"].startswith("http://127.0.0.1:") and req["redirect_uri"].endswith("/callback"))
        self.assertNotIn("at-1", out.getvalue())  # tokens are never printed

    def test_loopback_listener_flow(self):
        out = io.StringIO()
        result = {}

        def run():
            result["tokens"] = auth.login(token_path=self.path, client_id=CLIENT_ID, issuer=self.fake.url,
                                          open_browser=False, listen=True, input_fn=None, out=out, timeout_s=10)
        t = threading.Thread(target=run)
        t.start()
        deadline = time.time() + 5
        while "\n\n  " not in out.getvalue() and time.time() < deadline:
            time.sleep(0.02)
        url = out.getvalue().split("\n\n  ")[1].split("\n")[0]
        redirect = self.fake.approve(url)
        self.assertTrue(redirect.startswith("http://127.0.0.1:"))
        with urllib.request.urlopen(redirect, timeout=5) as r:
            self.assertEqual(r.status, 200)
        t.join(10)
        self.assertEqual(result["tokens"]["access_token"], "at-1")

    def test_listener_binds_loopback_only(self):
        results = __import__("queue").Queue()
        port = auth._free_loopback_port()
        server = auth._start_callback_listener(port, results)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.shutdown()
            server.server_close()

    def test_forged_state_aborts_login(self):
        def paste(prompt):
            return "http://127.0.0.1:9/callback?code=stolen&state=forged"
        with self.assertRaises(auth.StateMismatch):
            auth.login(token_path=self.path, client_id=CLIENT_ID, issuer=self.fake.url, open_browser=False,
                       listen=False, input_fn=paste, out=io.StringIO(), timeout_s=5)
        self.assertEqual(self.fake.token_requests, [])
        self.assertIsNone(auth.TokenStore(self.path).load())

    def test_wrong_verifier_rejected_by_server(self):
        url = auth.authorize_url(self.fake.url + "/x", CLIENT_ID, "http://127.0.0.1:1/callback", "user:inference",
                                 "s", auth.code_challenge("the-real-verifier-" + "a" * 30))
        code = urllib.parse.parse_qs(urllib.parse.urlparse(self.fake.approve(url)).query)["code"][0]
        with self.assertRaises(auth.AuthError) as ctx:
            auth._token_request(self.fake.url + "/api/oauth/token", {
                "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
                "redirect_uri": "http://127.0.0.1:1/callback", "code_verifier": "an-attacker-guess-" + "b" * 30})
        self.assertIn("invalid_grant", str(ctx.exception))


class TestTokenFile(AuthTestCase):
    def test_file_is_0600_and_dir_created(self):
        old = os.umask(0o022)
        try:
            auth.TokenStore(self.path).save(tokens())
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        auth.TokenStore(self.path).save(tokens(access="at-y"))  # rewrite keeps 0600
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.path))], ["cantonese-ai-token.json"])

    def test_corrupt_or_missing_file_is_not_signed_in(self):
        a = self.make_auth()
        with self.assertRaises(auth.NotSignedIn):
            a.authorization_header()
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{nope")
        with self.assertRaises(auth.NotSignedIn):
            a.authorization_header()

    def test_status_never_shows_secrets(self):
        auth.TokenStore(self.path).save(tokens(access="at-secret", refresh="rt-secret"))
        text = auth.status(self.path)
        self.assertIn("signed in", text)
        self.assertNotIn("secret", text)
        self.assertEqual(auth.status(self.path, api_key="sk-abc"), "using CANTONESE_AI_API_KEY")


class TestRefresh(AuthTestCase):
    def seed(self, expires_in=3600):
        self.fake.access.add("at-0")
        self.fake.refresh.add("rt-0")
        auth.TokenStore(self.path).save(tokens("at-0", "rt-0", expires_in))

    def test_api_key_wins(self):
        self.seed()
        a = self.make_auth(api_key="sk-abc")
        self.assertEqual((a.mode, a.authorization_header()), ("api_key", "Bearer sk-abc"))
        self.assertFalse(a.handle_unauthorized("Bearer sk-abc"))

    def test_valid_token_used_as_is(self):
        self.seed()
        a = self.make_auth()
        self.assertEqual((a.mode, a.authorization_header()), ("oauth", "Bearer at-0"))
        self.assertEqual(self.fake.token_requests, [])

    def test_refresh_before_expiry_and_rotation_saved(self):
        self.seed(expires_in=30)  # inside the refresh margin
        a = self.make_auth()
        self.assertEqual(a.authorization_header(), "Bearer at-1")
        [req] = self.fake.token_requests
        self.assertEqual((req["grant_type"], req["refresh_token"], req["client_id"]), ("refresh_token", "rt-0", CLIENT_ID))
        self.assertNotIn("client_secret", req)
        saved = auth.TokenStore(self.path).load()
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("at-1", "rt-1"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # The rotated (old) refresh token is dead; the saved new one keeps working.
        self.fake.expires_in = 30
        auth.TokenStore(self.path).save({**saved, "expires_at": time.time()})
        self.assertEqual(self.make_auth().authorization_header(), "Bearer at-2")
        self.assertEqual(auth.TokenStore(self.path).load()["refresh_token"], "rt-2")

    def test_refresh_keeps_old_refresh_token_if_none_returned(self):
        new = auth._tokens_from_response({"access_token": "a", "expires_in": 60}, previous={"refresh_token": "keep"})
        self.assertEqual(new["refresh_token"], "keep")

    def test_401_refreshes_once(self):
        self.seed()
        a = self.make_auth()
        header = a.authorization_header()
        self.assertTrue(a.handle_unauthorized(header))
        self.assertEqual(a.authorization_header(), "Bearer at-1")
        # A second 401 for the old header does not refresh again (already done).
        self.assertTrue(a.handle_unauthorized(header))
        self.assertEqual(len(self.fake.token_requests), 1)

    def test_revoked_refresh_token_means_sign_in_again(self):
        self.seed(expires_in=0)
        self.fake.refresh.clear()
        with self.assertRaises(auth.NotSignedIn) as ctx:
            self.make_auth().authorization_header()
        self.assertIn("login", str(ctx.exception))

    def test_token_written_by_login_is_picked_up_without_restart(self):
        a = self.make_auth()
        with self.assertRaises(auth.NotSignedIn):
            a.authorization_header()
        self.seed()
        self.assertEqual(a.authorization_header(), "Bearer at-0")

    def test_concurrent_refresh_uses_one_rotation(self):
        self.seed(expires_in=0)
        a = self.make_auth()
        headers, errors = [], []

        def run():
            try:
                headers.append(a.authorization_header())
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=run) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(set(headers), {"Bearer at-1"})
        self.assertEqual(len(self.fake.token_requests), 1)

    def test_tokens_never_logged(self):
        self.seed(expires_in=0)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("cantonese-ai-auth")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            self.make_auth().authorization_header()
            self.fake.refresh.clear()
            auth.TokenStore(self.path).save(tokens("at-1", "rt-1", 0))
            with self.assertRaises(auth.NotSignedIn) as ctx:
                self.make_auth().authorization_header()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(logging.CRITICAL)
        text = stream.getvalue() + str(ctx.exception)
        for secret in ("at-0", "rt-0", "at-1", "rt-1"):
            self.assertNotIn(secret, text)


class TestAppSignedIn(AuthTestCase):
    """app.transcribe() with a signed-in token against the fake /api/stt."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("WWEBJS_API_KEY", "test-key")
        cls.app = importlib.import_module("app")
        cls.app.log.setLevel("CRITICAL")

    def setUp(self):
        super().setUp()
        app = self.app
        app._auth, app.CANTONESE_AI_API_KEY = None, ""
        app.CANTONESE_AI_TOKEN_PATH, app.CANTONESE_AI_ISSUER = self.path, self.fake.url
        app.CANTONESE_AI_OAUTH_CLIENT_ID = CLIENT_ID
        app.STT_URL, app.STT_TIMEOUT_S = self.fake.url + "/api/stt", 5.0
        self.addCleanup(setattr, app, "_auth", None)

    def test_expired_on_server_401_then_refresh_and_retry(self):
        self.fake.refresh.add("rt-0")
        # Locally still valid, but the server no longer accepts at-0 (e.g. revoked early).
        auth.TokenStore(self.path).save(tokens("at-0", "rt-0", 3600))
        self.assertEqual(self.app.transcribe(b"AUDIO", "audio/ogg"), "你好")
        self.assertEqual(self.fake.stt_auth, ["Bearer at-0", "Bearer at-1"])
        self.assertEqual(auth.TokenStore(self.path).load()["refresh_token"], "rt-1")

    def test_persistent_401_is_permanent_after_one_retry(self):
        self.fake.refresh.add("rt-0")
        self.fake.reject_all_stt = True
        auth.TokenStore(self.path).save(tokens("at-0", "rt-0", 3600))
        with self.assertRaises(self.app.PermanentError):
            self.app.transcribe(b"AUDIO", "audio/ogg")
        self.assertEqual(len(self.fake.stt_auth), 2)

    def test_not_signed_in_keeps_job_queued(self):
        with self.assertRaises(self.app.TransientError) as ctx:
            self.app.transcribe(b"AUDIO", "audio/ogg")
        self.assertIn("login", str(ctx.exception))
        self.assertEqual(self.fake.stt_auth, [])

    def test_api_key_401_not_retried(self):
        self.app.CANTONESE_AI_API_KEY = "sk-wrong"
        with self.assertRaises(self.app.PermanentError):
            self.app.transcribe(b"AUDIO", "audio/ogg")
        self.assertEqual(self.fake.stt_auth, ["Bearer sk-wrong"])
        self.assertEqual(self.fake.token_requests, [])


if __name__ == "__main__":
    unittest.main()
