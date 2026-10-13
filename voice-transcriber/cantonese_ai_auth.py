"""Credentials for the cantonese.ai API. Stdlib only.

Two ways to authenticate, the API key wins when both are configured:

1. CANTONESE_AI_API_KEY: an "sk-..." key from https://cantonese.ai/api-keys, sent as
   "Authorization: Bearer <key>".
2. "Sign in with cantonese.ai": OAuth 2.0 authorization code flow for the confidential client
   `whatsapp-agent`, run once with `python app.py login`. The client secret comes from
   CANTONESE_AI_OAUTH_CLIENT_SECRET (given to each install by the setup bot / the owner, never
   in the repository) and is sent as the `client_secret` form field on the code exchange and on
   every refresh. A PKCE (S256) challenge is sent too. The only registered redirect URI is
   http://127.0.0.1:53682/callback, so the callback port is fixed (CANTONESE_AI_OAUTH_CALLBACK_PORT).
   The access and refresh tokens are kept in a 0600 JSON file (default
   /data/cantonese-ai-token.json) and refreshed before they expire and whenever the API answers
   401. Refresh tokens rotate, so the new one is always saved.

The client secret and the tokens are never logged or printed.
"""
import base64
import hashlib
import http.server
import json
import logging
import os
import queue
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

DEFAULT_ISSUER = "https://cantonese.ai"
DEFAULT_CLIENT_ID = "whatsapp-agent"
DEFAULT_SCOPE = "user:inference"
DEFAULT_TOKEN_PATH = "/data/cantonese-ai-token.json"
# cantonese.ai matches redirect URIs exactly: http://127.0.0.1:53682/callback is the registered one.
DEFAULT_CALLBACK_PORT = 53682
DEFAULT_CALLBACK_BIND = "127.0.0.1"
# The metadata document is optional (cantonese.ai has none): give up on it quickly.
DISCOVERY_TIMEOUT_S = 5
MISSING_SECRET_MESSAGE = (
    "CANTONESE_AI_OAUTH_CLIENT_SECRET is not set. Sign-in with cantonese.ai needs the client secret "
    "that the setup bot (or the owner of this install) provides: put it in .env (never in the "
    "repository), or set CANTONESE_AI_API_KEY instead.")
# Refresh this many seconds before the access token expires.
REFRESH_MARGIN_S = 120
HTTP_TIMEOUT_S = 30

log = logging.getLogger("cantonese-ai-auth")


class AuthError(Exception):
    """Signing in or refreshing failed."""


class NotSignedIn(AuthError):
    """No API key and no usable sign-in: run `python app.py login`."""


class StateMismatch(AuthError):
    """The callback's state does not match the one we sent (possible CSRF)."""


class MissingClientSecret(AuthError):
    """CANTONESE_AI_OAUTH_CLIENT_SECRET is not configured."""


def _require_secret(client_secret):
    secret = (client_secret or "").strip()
    if not secret:
        raise MissingClientSecret(MISSING_SECRET_MESSAGE)
    return secret


def redirect_uri_for(port=DEFAULT_CALLBACK_PORT):
    return f"http://127.0.0.1:{int(port)}/callback"


# ---------------------------------------------------------------- PKCE / URLs

def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def code_challenge(verifier):
    """RFC 7636 S256: BASE64URL(SHA256(ASCII(code_verifier)))."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_pkce_pair():
    verifier = _b64url(secrets.token_bytes(48))  # 64 characters, within RFC 7636's 43..128
    return verifier, code_challenge(verifier)


def discover(issuer=DEFAULT_ISSUER, timeout=DISCOVERY_TIMEOUT_S):
    """Authorization and token endpoints from RFC 8414 metadata, with fixed fallbacks."""
    issuer = issuer.rstrip("/")
    endpoints = {"authorization_endpoint": f"{issuer}/api/oauth/authorize",
                 "token_endpoint": f"{issuer}/api/oauth/token"}
    try:
        with urllib.request.urlopen(f"{issuer}/.well-known/oauth-authorization-server", timeout=timeout) as r:
            meta = json.loads(r.read())
        for key in endpoints:
            if isinstance(meta.get(key), str) and meta[key].startswith(("https://", "http://")):
                endpoints[key] = meta[key]
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.debug("no OAuth metadata at %s (%s); using default endpoints", issuer, e)
    return endpoints


def authorize_url(endpoint, client_id, redirect_uri, scope, state, challenge):
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scope,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"


def parse_callback(url_or_query, expected_state):
    """Return the authorization code from the redirect URL (or its query string).
    Raises StateMismatch if state differs, AuthError if the server returned an error."""
    text = (url_or_query or "").strip()
    query = urllib.parse.urlparse(text).query if "://" in text or text.startswith("/") else text.lstrip("?")
    params = urllib.parse.parse_qs(query)
    state = (params.get("state") or [""])[0]
    if not secrets.compare_digest(state.encode(), expected_state.encode()):
        raise StateMismatch("state does not match; start the sign-in again")
    if params.get("error"):
        desc = (params.get("error_description") or [""])[0]
        raise AuthError(f"sign-in refused: {params['error'][0]} {desc}".strip())
    code = (params.get("code") or [""])[0]
    if not code:
        raise AuthError("the URL has no authorization code")
    return code


# ---------------------------------------------------------------- token endpoint

def _token_request(token_endpoint, fields, timeout=HTTP_TIMEOUT_S):
    req = urllib.request.Request(token_endpoint, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"content-type": "application/x-www-form-urlencoded",
                                          "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Only the standard error fields: the body could echo something sensitive.
        try:
            err = json.loads(e.read())
            detail = f"{err.get('error', '')} {err.get('error_description', '')}".strip()
        except ValueError:
            detail = ""
        exc = AuthError(f"token endpoint answered {e.code}" + (f": {detail}" if detail else ""))
        exc.status, exc.oauth_error = e.code, (detail.split(" ")[0] if detail else "")
        raise exc from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        exc = AuthError(f"token endpoint unreachable: {type(e).__name__}")
        exc.status, exc.oauth_error = None, ""
        raise exc from None
    if not isinstance(body, dict) or not body.get("access_token"):
        raise AuthError("token endpoint returned no access_token")
    return body


def _tokens_from_response(body, previous=None, now=None):
    now = time.time() if now is None else now
    try:
        expires_in = float(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    return {
        "access_token": body["access_token"],
        # Rotation: always keep the newest refresh token; keep the old one only if none was sent.
        "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": now + expires_in,
        "scope": body.get("scope") or (previous or {}).get("scope"),
        "token_type": body.get("token_type") or "Bearer",
        "obtained_at": now,
    }


# ---------------------------------------------------------------- storage

class TokenStore:
    """JSON file readable only by its owner (0600), written atomically."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) and data.get("access_token") else None
        except (OSError, ValueError):
            return None

    def mtime(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def save(self, tokens):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cantonese-ai-token.", dir=directory)  # mkstemp creates 0600
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tokens, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self):
        try:
            os.unlink(self.path)
            return True
        except FileNotFoundError:
            return False


# ---------------------------------------------------------------- runtime credential

class CantoneseAiAuth:
    """Gives the Authorization header for API calls; thread-safe. Re-reads the token file
    when it changes, so `login` can run while the service is up."""

    def __init__(self, api_key=None, token_path=DEFAULT_TOKEN_PATH, client_id=DEFAULT_CLIENT_ID,
                 issuer=DEFAULT_ISSUER, token_endpoint=None, client_secret=None):
        self.api_key = (api_key or "").strip() or None
        self.store = TokenStore(token_path)
        self.client_id = client_id
        self._client_secret = (client_secret or "").strip() or None
        self.issuer = issuer
        self._token_endpoint = token_endpoint
        self._lock = threading.Lock()
        self._tokens = None
        self._mtime = None

    @property
    def mode(self):
        if self.api_key:
            return "api_key"
        return "oauth" if self._current() else "none"

    def token_endpoint(self):
        if not self._token_endpoint:
            self._token_endpoint = discover(self.issuer)["token_endpoint"]
        return self._token_endpoint

    def _current(self):
        mtime = self.store.mtime()
        if mtime != self._mtime:
            self._tokens, self._mtime = self.store.load(), mtime
        return self._tokens

    def authorization_header(self):
        if self.api_key:
            return f"Bearer {self.api_key}"
        with self._lock:
            tokens = self._current()
            if not tokens:
                raise NotSignedIn("not signed in to cantonese.ai: set CANTONESE_AI_API_KEY or run `python app.py login`")
            if tokens.get("expires_at", 0) - REFRESH_MARGIN_S <= time.time() and tokens.get("refresh_token"):
                tokens = self._refresh_locked(tokens)
            return f"Bearer {tokens['access_token']}"

    def handle_unauthorized(self, rejected_header):
        """Called after a 401. Returns True if a new credential is available (retry once)."""
        if self.api_key:
            return False
        with self._lock:
            tokens = self._current()
            if not tokens:
                return False
            if f"Bearer {tokens['access_token']}" != rejected_header:
                return True  # someone already refreshed (another thread or process)
            if not tokens.get("refresh_token"):
                return False
            self._refresh_locked(tokens)
            return True

    def __repr__(self):
        return f"CantoneseAiAuth(mode={self.mode!r}, client_id={self.client_id!r})"  # no secrets

    def _refresh_locked(self, tokens):
        try:
            secret = _require_secret(self._client_secret)
        except MissingClientSecret as e:
            raise NotSignedIn(f"cannot refresh the cantonese.ai sign-in: {e}") from None
        try:
            body = _token_request(self.token_endpoint(), {
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": self.client_id, "client_secret": secret})
        except AuthError as e:
            # Another process may have rotated the refresh token under us; use its result.
            fresh = self.store.load()
            if fresh and fresh.get("refresh_token") != tokens.get("refresh_token"):
                self._tokens, self._mtime = fresh, self.store.mtime()
                return fresh
            if getattr(e, "oauth_error", "") == "invalid_client":
                raise NotSignedIn("cantonese.ai rejected the OAuth client credentials (invalid_client): check "
                                  "CANTONESE_AI_OAUTH_CLIENT_ID and CANTONESE_AI_OAUTH_CLIENT_SECRET") from None
            if getattr(e, "status", None) in (400, 401):
                raise NotSignedIn(f"cantonese.ai sign-in expired or revoked ({e}); run `python app.py login` again") from None
            raise
        new = _tokens_from_response(body, previous=tokens)
        self.store.save(new)
        self._tokens, self._mtime = new, self.store.mtime()
        log.info("refreshed the cantonese.ai access token (valid for %.0f min)", (new["expires_at"] - time.time()) / 60)
        return new


# ---------------------------------------------------------------- interactive login

class _CallbackServer(http.server.HTTPServer):
    # No SO_REUSEADDR: a port that is in use must fail loudly, since the redirect URI is fixed.
    allow_reuse_address = False


def _start_callback_listener(port, results, bind=DEFAULT_CALLBACK_BIND):
    """Listen for the redirect on bind:port. bind is 127.0.0.1 except inside a container, where
    0.0.0.0 lets the host's published port (127.0.0.1:53682 on the host) reach it."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            results.put(("callback", self.path))
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<!doctype html><title>Signed in</title><p>You can close this tab and return to "
                             "the terminal.</p>".encode())

        def log_message(self, *args):
            pass  # the request line contains the authorization code

    try:
        server = _CallbackServer((bind or DEFAULT_CALLBACK_BIND, int(port)), Handler)
    except OSError as e:
        raise AuthError(
            f"cannot listen on {bind}:{port} for the sign-in callback ({e.strerror or e}). The registered "
            f"redirect URI is {redirect_uri_for(port)}, so the port cannot change: stop whatever uses port "
            f"{port} (another login?) and try again, or use `login --no-listen` and paste the redirected URL."
        ) from None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def login(token_path=DEFAULT_TOKEN_PATH, client_id=DEFAULT_CLIENT_ID, issuer=DEFAULT_ISSUER, scope=DEFAULT_SCOPE,
          open_browser=True, listen=True, input_fn=input, out=None, timeout_s=600, port=DEFAULT_CALLBACK_PORT,
          endpoints=None, client_secret=None, bind=DEFAULT_CALLBACK_BIND):
    """Run the authorization code flow (confidential client, plus PKCE) and save the tokens.
    Returns the saved tokens.

    The sign-in URL is printed (and opened in a browser when possible). The code comes back
    either to a listener on bind:port (redirect URI http://127.0.0.1:<port>/callback), or - on a
    headless server - from the user pasting the URL their browser ended up on."""
    out = out or sys.stdout
    secret = _require_secret(client_secret)  # fail before the user goes through the browser
    port = int(port or DEFAULT_CALLBACK_PORT)
    redirect_uri = redirect_uri_for(port)
    endpoints = endpoints or discover(issuer)
    verifier, challenge = new_pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(endpoints["authorization_endpoint"], client_id, redirect_uri, scope, state, challenge)

    results = queue.Queue()
    server = _start_callback_listener(port, results, bind) if listen else None
    try:
        print("Sign in with cantonese.ai by opening this URL in a browser:\n\n  " + url + "\n", file=out, flush=True)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        print("After you approve, the browser goes to " + redirect_uri + " ...\n"
              "If that page does not load (headless server, another computer), copy the full\n"
              "address from the browser's address bar and paste it here.", file=out, flush=True)

        def read_paste():
            try:
                results.put(("paste", input_fn("Redirected URL: ")))
            except (EOFError, KeyboardInterrupt, OSError):
                pass

        if input_fn is not None:
            threading.Thread(target=read_paste, daemon=True).start()
        try:
            _, value = results.get(timeout=timeout_s)
        except queue.Empty:
            raise AuthError("timed out waiting for the sign-in") from None
        code = parse_callback(value, state)
    finally:
        if server:
            server.shutdown()
            server.server_close()

    body = _token_request(endpoints["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "client_secret": secret, "code_verifier": verifier})
    tokens = _tokens_from_response(body)
    TokenStore(token_path).save(tokens)
    print(f"Signed in. Token saved to {token_path} (readable only by this user).", file=out, flush=True)
    return tokens


def status(token_path=DEFAULT_TOKEN_PATH, api_key=None, client_secret=None):
    """A human-readable credential summary without any secret in it. When client_secret is
    passed (even empty), it also says whether one is configured: yes/no only."""
    secret_note = ""
    if client_secret is not None:
        secret_note = f"; OAuth client secret configured: {'yes' if client_secret.strip() else 'no'}"
    if (api_key or "").strip():
        return "using CANTONESE_AI_API_KEY" + secret_note
    tokens = TokenStore(token_path).load()
    if not tokens:
        return "not signed in (set CANTONESE_AI_API_KEY or run `python app.py login`)" + secret_note
    left = tokens.get("expires_at", 0) - time.time()
    refresh = "with" if tokens.get("refresh_token") else "without"
    return (f"signed in with cantonese.ai (scope {tokens.get('scope') or 'unknown'}), access token "
            f"{'expires in %.0f min' % (left / 60) if left > 0 else 'expired'}, {refresh} a refresh token"
            + secret_note)
