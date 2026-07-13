# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Dependency-free client for the ChatGPT **Codex** backend (subscription auth).

The Codex CLI, when logged in with a ChatGPT subscription (not an API key),
talks to ``https://chatgpt.com/backend-api/codex/responses`` — a Responses-API
shaped SSE endpoint billed against the ChatGPT plan. The native Codex provider
talks to it directly from Python (stdlib ``urllib`` + ``ssl`` only, mirroring
the native Gemini/Z.AI clients) and runs the agent tool-calling loop
in-process, so no ``codex`` binary is needed.

Auth is the CLI's own ``$CODEX_HOME/auth.json``: OAuth tokens from a ChatGPT
login (``tokens.access_token`` / ``refresh_token``), refreshed against
``https://auth.openai.com/oauth/token`` with the Codex CLI's public client id.
This module also reimplements the CLI's device-code login flow so users can
authenticate without the binary (see ``device_code_start`` /
``device_code_poll_and_exchange``).

The endpoint is stateless: ``store`` must be false and the client replays the
full transcript (including ``reasoning`` items with encrypted content and
``function_call``/``function_call_output`` items) on every request.

The client is intentionally Odoo-free so it can be unit-tested standalone.
"""
import base64
import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# The ChatGPT-subscription Codex backend (NOT api.openai.com — that side is
# API-key billed). The Responses endpoint lives under this base.
CHATGPT_CODEX_BASE = "https://chatgpt.com/backend-api/codex"

# OAuth constants shipped in the open-source Codex CLI (public client).
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEVICE_AUTH_ISSUER = "https://auth.openai.com"
# JWT claim namespace holding the ChatGPT account id / plan type.
JWT_AUTH_CLAIM = "https://api.openai.com/auth"

# Sent as both the ``originator`` header and the User-Agent prefix — the
# backend (behind Cloudflare) is picky about unusual header sets, so we present
# exactly like the Rust CLI does.
ORIGINATOR = "codex_cli_rs"
USER_AGENT = "codex_cli_rs/0.42.0 (aidoo; Linux) terminal_unknown"

# Refresh thresholds mirroring the CLI: refresh when the access token expires
# within 5 minutes, or the last refresh is older than 8 days.
_EXPIRY_SLACK_S = 300
_STALE_REFRESH_S = 8 * 24 * 3600


def jwt_claims(token):
    """Decode a JWT's payload segment (no signature verification).

    Returns ``{}`` for anything that does not look like a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — malformed token is just "no claims"
        return {}


def _utc_now_rfc3339():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _parse_rfc3339(value):
    """Epoch seconds for an RFC3339 UTC timestamp, or None. Lenient: accepts
    fractional seconds and a Z/offset suffix (codex writes chrono's format)."""
    if not isinstance(value, str):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", value)
    if not m:
        return None
    try:
        import calendar
        st = time.strptime("%s %s" % (m.group(1), m.group(2)),
                           "%Y-%m-%d %H:%M:%S")
        return calendar.timegm(st)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Proactive client-side rate limiting (token bucket), shared per quota key
# across all runs in THIS Odoo worker process (same as the z.ai client).
# ---------------------------------------------------------------------------
_LIMITERS = {}
_LIMITERS_LOCK = threading.Lock()


class _TokenBucket:
    def __init__(self, rpm):
        self.capacity = float(max(1, rpm))
        self.rate = self.capacity / 60.0      # tokens per second
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        """Block until a token is available; return seconds waited."""
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited
                deficit = (1.0 - self.tokens) / self.rate
            nap = min(deficit, 0.5)
            time.sleep(nap)
            waited += nap


def _get_limiter(key, rpm):
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(key)
        if lim is None or lim.capacity != float(max(1, rpm)):
            lim = _TokenBucket(rpm)
            _LIMITERS[key] = lim
        return lim


class CodexError(Exception):
    """Raised for any non-2xx / transport failure talking to the backend.

    ``status`` is the HTTP status when known; ``is_rate_limit`` is True for 429
    so callers can branch (e.g. surface a retry notice)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status
        self.is_rate_limit = status == 429


class CodexAuthError(CodexError):
    """Credentials are missing/expired and could not be refreshed."""


class CodexClient:
    """Talks to the ChatGPT Codex Responses endpoint with subscription OAuth.

    ``auth`` is the parsed ``$CODEX_HOME/auth.json`` dict (the WHOLE file, with
    the nested ``tokens`` object). Refreshed tokens are written back into that
    dict in place and persisted through the optional ``save_auth(dict)``
    callback — unknown keys in the file are preserved."""

    _MAX_ATTEMPTS = 6           # total tries (>= 2)
    _INITIAL_DELAY_S = 5
    _MAX_DELAY_S = 30           # cap for the exponential fallback
    _HINT_CAP_S = 90            # absolute cap when honoring a server reset hint
    _MAX_NET_RETRIES = 2        # transient network blips only

    def __init__(self, auth, save_auth=None, base_url=CHATGPT_CODEX_BASE,
                 timeout=120, on_retry=None, rpm=0, deadline=None):
        self._auth = auth if isinstance(auth, dict) else {}
        self._save = save_auth
        self._base = (base_url or CHATGPT_CODEX_BASE).rstrip("/")
        self._timeout = timeout
        # Optional callback(attempt:int, delay_s:float, reason) invoked before
        # each backoff wait, so the caller can surface a "retrying in Ns" notice.
        self._on_retry = on_retry
        self._rpm = int(rpm or 0)
        # Optional wall-clock deadline (epoch seconds, as from ``time.time()``):
        # the caller's turn timeout. We refuse to start a backoff wait that would
        # run past it, so retries can never block the run beyond its deadline.
        self._deadline = deadline
        # Quota key: the account, not the token — tokens rotate on refresh.
        self._quota_key = str(hash(self._tokens().get("refresh_token") or ""))
        self._ctx = ssl.create_default_context()

    # ------------------------------------------------------------------
    # OAuth: access-token lifecycle over auth.json
    # ------------------------------------------------------------------
    def _tokens(self):
        tokens = self._auth.get("tokens")
        return tokens if isinstance(tokens, dict) else {}

    def _needs_refresh(self):
        tok = self._tokens().get("access_token")
        if not tok:
            return True
        exp = jwt_claims(tok).get("exp")
        if isinstance(exp, (int, float)):
            return (exp - time.time()) < _EXPIRY_SLACK_S
        last = _parse_rfc3339(self._auth.get("last_refresh"))
        if last is None:
            return True
        return (time.time() - last) > _STALE_REFRESH_S

    def access_token(self):
        """A valid access token, refreshing in place when expired/near expiry."""
        if self._needs_refresh():
            return self._refresh()
        return self._tokens().get("access_token")

    def _refresh(self):
        rt = self._tokens().get("refresh_token")
        if not rt:
            raise CodexAuthError(
                "No refresh_token in stored credentials; reconnect Codex "
                "with your ChatGPT account.")
        body = json.dumps({
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "scope": "openid profile email",
        }).encode()
        try:
            raw = self._request(OAUTH_TOKEN_URL, data=body,
                                headers={"Content-Type": "application/json"})
        except CodexError as e:
            raise CodexAuthError(
                "Codex token refresh failed (%s); reconnect Codex with your "
                "ChatGPT account." % e) from e
        data = json.loads(raw or "{}")
        if not data.get("access_token"):
            raise CodexAuthError("Codex token refresh returned no access_token.")
        tokens = self._auth.setdefault("tokens", {})
        tokens["access_token"] = data["access_token"]
        if data.get("id_token"):
            tokens["id_token"] = data["id_token"]
        # The refresh token may rotate — always keep the newest one.
        if data.get("refresh_token"):
            tokens["refresh_token"] = data["refresh_token"]
        self._auth["last_refresh"] = _utc_now_rfc3339()
        if self._save:
            try:
                self._save(dict(self._auth))
            except Exception:  # noqa: BLE001 — persistence is best-effort
                pass
        return tokens["access_token"]

    def account_id(self):
        """The ChatGPT account id for the ``chatgpt-account-id`` header."""
        tokens = self._tokens()
        if tokens.get("account_id"):
            return tokens["account_id"]
        for tok in (tokens.get("id_token"), tokens.get("access_token")):
            if not tok:
                continue
            claim = jwt_claims(tok).get(JWT_AUTH_CLAIM) or {}
            if isinstance(claim, dict) and claim.get("chatgpt_account_id"):
                return claim["chatgpt_account_id"]
        raise CodexAuthError(
            "Could not determine the ChatGPT account id from the stored "
            "credentials; reconnect Codex with your ChatGPT account.")

    def plan_type(self):
        """The ChatGPT plan (e.g. 'plus', 'pro') — diagnostics only."""
        for tok in (self._tokens().get("id_token"),
                    self._tokens().get("access_token")):
            if not tok:
                continue
            claim = jwt_claims(tok).get(JWT_AUTH_CLAIM) or {}
            if isinstance(claim, dict) and claim.get("chatgpt_plan_type"):
                return claim["chatgpt_plan_type"]
        return None

    def _headers(self, session_id=None):
        return {
            "Authorization": "Bearer " + (self.access_token() or ""),
            "chatgpt-account-id": self.account_id(),
            "OpenAI-Beta": "responses=experimental",
            "originator": ORIGINATOR,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **({"session_id": session_id} if session_id else {}),
        }

    # ------------------------------------------------------------------
    # Low-level HTTP (stdlib only; no gzip so SSE streams arrive raw)
    # ------------------------------------------------------------------
    def _request(self, url, *, data=None, headers=None, method="POST", stream=False):
        if self._rpm:
            _get_limiter(self._quota_key, self._rpm).acquire()
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            try:
                resp = urllib.request.urlopen(req, context=self._ctx,
                                              timeout=self._timeout)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:2000]
                except Exception:  # noqa: BLE001
                    pass
                if e.code in (429, 503) and attempt < self._MAX_ATTEMPTS - 1:
                    delay = self._retry_delay(e.headers, body, attempt)
                    if self._can_wait(delay):
                        self._notify_retry(attempt + 1, delay, e.code)
                        time.sleep(delay)
                        attempt += 1
                        continue
                raise CodexError(
                    "HTTP %s for %s: %s" % (e.code, url, body), status=e.code)
            except (urllib.error.URLError, OSError) as e:
                if attempt < self._MAX_NET_RETRIES:
                    delay = self._backoff(attempt)
                    if self._can_wait(delay):
                        self._notify_retry(attempt + 1, delay, "network")
                        time.sleep(delay)
                        attempt += 1
                        continue
                raise CodexError("Network error for %s: %s" % (url, e))
            if stream:
                return resp
            with resp:
                return resp.read().decode("utf-8", "replace")

    def _can_wait(self, delay):
        """True if a backoff of ``delay`` seconds finishes before the caller's
        turn deadline (if one was supplied). Keeps total retry time bounded so a
        long server backoff can never overrun the run's wall-clock budget."""
        if not self._deadline:
            return True
        return (time.time() + delay) < self._deadline

    def _notify_retry(self, attempt, delay, reason):
        if self._on_retry:
            try:
                self._on_retry(attempt, delay, reason)
            except Exception:  # noqa: BLE001 — a status callback must never break the run
                pass

    @classmethod
    def _backoff(cls, attempt):
        """Exponential backoff (5s, 10s, 20s, …) capped at _MAX_DELAY_S, jittered."""
        base = min(cls._INITIAL_DELAY_S * (2 ** attempt), cls._MAX_DELAY_S)
        return round(base * (0.8 + 0.4 * random.random()), 1)

    @classmethod
    def _retry_delay(cls, resp_headers, body, attempt):
        """Seconds to wait before a 429/503 retry: Retry-After header -> body
        "try again in Ns" hint -> exponential backoff."""
        ra = None
        try:
            ra = resp_headers.get("Retry-After") if resp_headers else None
        except Exception:  # noqa: BLE001
            ra = None
        if ra:
            try:
                return min(int(float(ra)), cls._HINT_CAP_S)
            except (TypeError, ValueError):
                pass  # HTTP-date form — ignore, fall through
        m = re.search(r"(?:try again|reset(?:s)?) (?:after|in) (\d+(?:\.\d+)?)\s*s",
                      body or "", re.IGNORECASE)
        if m:
            return min(int(float(m.group(1))) + 1, cls._HINT_CAP_S)
        return cls._backoff(attempt)

    # ------------------------------------------------------------------
    # Streaming Responses API (SSE)
    # ------------------------------------------------------------------
    def stream_responses(self, model, input_items, *, instructions, tools=None,
                         reasoning=None, session_id=None, prompt_cache_key=None):
        """POST /responses with stream:true; yield parsed SSE event dicts.

        Each yielded dict carries its event name in ``"type"``
        (``response.output_text.delta``, ``response.output_item.done``,
        ``response.completed``, ``response.failed``, …).

        The endpoint requires ``instructions`` and ``store:false``;
        ``include: reasoning.encrypted_content`` lets the replayed ``reasoning``
        items keep multi-round coherence in this stateless mode."""
        payload = {
            "model": model,
            "instructions": instructions or "",
            "input": input_items,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if tools:
            payload["tools"] = tools
        if reasoning:
            payload["reasoning"] = reasoning
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        url = self._base + "/responses"
        # default=str: tool results fed back as function_call_output may carry
        # datetime/Decimal/bytes from Odoo .read() — serialize as strings.
        body = json.dumps(payload, default=str).encode()
        resp = None
        for auth_attempt in (0, 1):
            try:
                resp = self._request(url, data=body,
                                     headers=self._headers(session_id),
                                     stream=True)
                break
            except CodexError as e:
                # One forced refresh on 401: the stored token may have been
                # revoked/rotated elsewhere while still looking unexpired.
                if e.status == 401 and auth_attempt == 0:
                    self._refresh()
                    continue
                raise
        with resp:
            for obj in self._iter_sse(resp):
                yield obj

    @staticmethod
    def _iter_sse(resp):
        """Parse an SSE byte stream into JSON objects (one per ``data:`` event).

        The event name is not taken from the ``event:`` line — the Responses
        API repeats it in the data JSON's ``type`` field, which is what callers
        dispatch on."""
        buf = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":  # event boundary
                if buf:
                    payload = "\n".join(buf).strip()
                    buf = []
                    if payload and payload != "[DONE]":
                        try:
                            yield json.loads(payload)
                        except ValueError:
                            pass
                continue
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())
            # ignore other SSE fields (event:, id:, :comments)
        if buf:  # trailing event without a final blank line
            payload = "\n".join(buf).strip()
            if payload and payload != "[DONE]":
                try:
                    yield json.loads(payload)
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# Device-code login (pure-Python reimplementation of `codex login --device-auth`)
# ---------------------------------------------------------------------------
def _device_post(url, payload, *, form=False, timeout=30):
    """One JSON(-or-form) POST for the device flow; returns (status, dict)."""
    if form:
        data = urllib.parse.urlencode(payload).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode()
        ctype = "application/json"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(
                req, context=ssl.create_default_context(),
                timeout=timeout) as resp:
            return resp.status, json.loads(
                resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"error": body}
    except (urllib.error.URLError, OSError) as e:
        raise CodexError("Network error for %s: %s" % (url, e))


def device_code_start(client_id=OAUTH_CLIENT_ID, issuer=DEVICE_AUTH_ISSUER):
    """Begin a device-code login; returns the info the UI must show.

    ``{"device_auth_id", "user_code", "interval", "verify_url"}`` — the user
    opens ``verify_url`` and enters ``user_code`` there."""
    status, data = _device_post(
        issuer + "/api/accounts/deviceauth/usercode", {"client_id": client_id})
    if status != 200 or not data.get("user_code"):
        raise CodexError(
            "Could not start the ChatGPT device login (HTTP %s): %s. Device-"
            "code login may be disabled for your account/workspace."
            % (status, json.dumps(data)[:300]), status=status)
    return {
        "device_auth_id": data.get("device_auth_id") or data.get("id"),
        "user_code": data["user_code"],
        "interval": int(data.get("interval") or 5),
        "verify_url": issuer + "/codex/device",
    }


def device_code_poll_and_exchange(device_auth_id, user_code, interval=5,
                                  client_id=OAUTH_CLIENT_ID,
                                  issuer=DEVICE_AUTH_ISSUER,
                                  timeout_s=900, cancel_event=None):
    """Poll until the user approves the device code, then exchange the
    authorization code for tokens. Returns a complete auth.json-shaped dict.

    Pending polls answer 403/404; the success payload carries the
    ``authorization_code`` plus the SERVER-generated PKCE ``code_verifier``
    (no client-side PKCE state), which we then exchange at /oauth/token."""
    deadline = time.time() + timeout_s
    grant = None
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise CodexError("Device login cancelled.")
        status, data = _device_post(
            issuer + "/api/accounts/deviceauth/token",
            {"device_auth_id": device_auth_id, "user_code": user_code})
        if status == 200 and data.get("authorization_code"):
            grant = data
            break
        if status not in (403, 404, 202, 428):
            raise CodexError(
                "Device login failed (HTTP %s): %s"
                % (status, json.dumps(data)[:300]), status=status)
        time.sleep(max(1, int(interval)))
    if not grant:
        raise CodexError("Device login timed out; please start again.")

    status, tokens = _device_post(
        issuer + "/oauth/token", {
            "grant_type": "authorization_code",
            "code": grant["authorization_code"],
            "redirect_uri": issuer + "/deviceauth/callback",
            "client_id": client_id,
            "code_verifier": grant.get("code_verifier") or "",
        }, form=True)
    if status != 200 or not tokens.get("access_token"):
        raise CodexAuthError(
            "Device-code token exchange failed (HTTP %s): %s"
            % (status, json.dumps(tokens)[:300]), status=status)

    account_id = None
    for tok in (tokens.get("id_token"), tokens.get("access_token")):
        if not tok:
            continue
        claim = jwt_claims(tok).get(JWT_AUTH_CLAIM) or {}
        if isinstance(claim, dict) and claim.get("chatgpt_account_id"):
            account_id = claim["chatgpt_account_id"]
            break
    auth = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": tokens.get("id_token"),
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "account_id": account_id,
        },
        "last_refresh": _utc_now_rfc3339(),
    }
    return auth
