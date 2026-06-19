# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Dependency-free client for the Gemini **Code Assist** API.

This is the same backend the ``gemini`` CLI uses for Google-login ("subscription")
auth — ``cloudcode-pa.googleapis.com/v1internal`` — but spoken directly from
Python with the standard library only (``urllib`` + ``ssl``). It exists because
in some egress environments the Node ``gemini`` CLI cannot complete HTTPS to
Google (responses are reset mid-body), while Python's HTTP stack works fine; the
native Gemini adapter drives this client instead of a CLI subprocess.

The client is intentionally Odoo-free so it can be unit-tested standalone against
the real API. It handles OAuth token refresh, Code Assist onboarding/project
resolution, and streaming ``generateContent`` (SSE) with function-calling.
"""
import json
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_API_VERSION = "v1internal"
# The gemini-cli public OAuth client id/secret (Code Assist). These are not
# secrets in the usual sense — they are shipped in the CLI and identify the
# application to Google's OAuth for the user-consented Code Assist scope.
OAUTH_CLIENT_ID = ("681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j"
                   ".apps.googleusercontent.com")
OAUTH_CLIENT_SECRET = "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"

# Client metadata sent on loadCodeAssist/onboardUser (mirrors the CLI's values).
DEFAULT_METADATA = {
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}


class CodeAssistError(Exception):
    """Raised for any non-2xx / transport failure talking to Google.

    ``status`` is the HTTP status when known; ``is_rate_limit`` is True for 429
    RESOURCE_EXHAUSTED so callers can branch (e.g. fall back to another model)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status
        self.is_rate_limit = status == 429


class CodeAssistClient:
    """Talks to the Code Assist API using imported Google-login OAuth creds.

    ``creds`` is the parsed ``oauth_creds.json`` dict (access_token,
    refresh_token, expiry_date in ms). ``save_creds`` is an optional callback
    invoked with the updated creds dict whenever the access token is refreshed,
    so the caller can persist it (mode 0600) for the next turn.
    """

    def __init__(self, creds, save_creds=None, project=None, timeout=120,
                 on_retry=None):
        self._creds = dict(creds or {})
        self._save = save_creds
        self._project = project or None
        self._timeout = timeout
        # Optional callback(attempt:int, delay_s:float, reason) invoked before
        # each backoff wait, so the caller can surface a "retrying in Ns" notice.
        self._on_retry = on_retry
        self._ctx = ssl.create_default_context()

    # ------------------------------------------------------------------
    # Low-level HTTP (stdlib only; no gzip so SSE streams arrive raw)
    # ------------------------------------------------------------------
    # Retry on 429 RESOURCE_EXHAUSTED / 503 with exponential backoff, mirroring
    # the node gemini CLI's retryWithBackoff (maxAttempts 10, 5s initial, ×2,
    # 30s cap, honors Retry-After). Code Assist standard-tier rate-limits hard;
    # waiting the quota-reset window turns transient 429s into success. Total
    # wait stays well under the run deadline (aidoo.timeout_s, 900s).
    _MAX_ATTEMPTS = 6           # total tries (>= 2)
    _INITIAL_DELAY_S = 5
    _MAX_DELAY_S = 30           # cap for the exponential fallback
    _HINT_CAP_S = 90           # absolute cap when honoring a server reset hint
    _MAX_NET_RETRIES = 2        # transient network blips only

    def _request(self, url, *, data=None, headers=None, method="POST", stream=False):
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
                    self._notify_retry(attempt + 1, delay, e.code)
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise CodeAssistError(
                    "HTTP %s for %s: %s" % (e.code, url, body), status=e.code)
            except (urllib.error.URLError, OSError) as e:
                # Transient network blip (ECONNRESET/ETIMEDOUT/…): a couple of
                # short retries; a real outage still fails fast.
                if attempt < self._MAX_NET_RETRIES:
                    delay = self._backoff(attempt)
                    self._notify_retry(attempt + 1, delay, "network")
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise CodeAssistError("Network error for %s: %s" % (url, e))
            if stream:
                return resp
            with resp:
                return resp.read().decode("utf-8", "replace")

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
        """Seconds to wait before a 429/503 retry. Priority: Retry-After header
        -> gRPC RetryInfo.retryDelay -> body "reset after Ns" -> exp backoff."""
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
        ri = cls._parse_retry_info(body)
        if ri is not None:
            return min(ri, cls._HINT_CAP_S)
        m = re.search(r"reset(?:s)? (?:after|in) (\d+)\s*s", body or "")
        if m:
            return min(int(m.group(1)) + 1, cls._HINT_CAP_S)
        return cls._backoff(attempt)

    @staticmethod
    def _parse_retry_info(body):
        """Extract RetryInfo.retryDelay (e.g. "57s") from a Google error body."""
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return None
        err = data.get("error") if isinstance(data, dict) else None
        for d in ((err or {}).get("details") or []):
            if isinstance(d, dict) and "RetryInfo" in (d.get("@type") or ""):
                rd = d.get("retryDelay")
                if isinstance(rd, str) and rd.endswith("s"):
                    try:
                        return int(float(rd[:-1])) + 1
                    except ValueError:
                        return None
        return None

    def _post_json(self, url, payload, headers):
        # default=str so tool results carrying non-JSON-native values (datetime,
        # date, Decimal, bytes) fed back as functionResponse parts serialize
        # cleanly — Odoo .read() returns real datetime objects.
        body = json.dumps(payload, default=str).encode()
        h = dict(headers or {}, **{"Content-Type": "application/json"})
        return json.loads(self._request(url, data=body, headers=h) or "{}")

    # ------------------------------------------------------------------
    # OAuth token refresh
    # ------------------------------------------------------------------
    def _access_token(self):
        """A valid access token, refreshing in place when expired/near expiry."""
        tok = self._creds.get("access_token")
        exp = self._creds.get("expiry_date")  # ms epoch
        if tok and isinstance(exp, (int, float)) and (exp - time.time() * 1000) > 60000:
            return tok
        return self._refresh()

    def _refresh(self):
        rt = self._creds.get("refresh_token")
        if not rt:
            raise CodeAssistError(
                "No refresh_token in stored credentials; re-import oauth_creds.json.")
        form = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
        }).encode()
        raw = self._request(
            OAUTH_TOKEN_URL, data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        data = json.loads(raw or "{}")
        if not data.get("access_token"):
            raise CodeAssistError("Token refresh returned no access_token.")
        self._creds["access_token"] = data["access_token"]
        self._creds["expiry_date"] = int(
            time.time() * 1000 + int(data.get("expires_in", 3600)) * 1000)
        if data.get("id_token"):
            self._creds["id_token"] = data["id_token"]
        # Google does not re-issue the refresh_token on refresh; keep the old one.
        if self._save:
            try:
                self._save(dict(self._creds))
            except Exception:  # noqa: BLE001 — persistence is best-effort
                pass
        return self._creds["access_token"]

    def _headers(self):
        return {"Authorization": "Bearer " + self._access_token()}

    def _method_url(self, method, query=None):
        url = "%s/%s:%s" % (CODE_ASSIST_ENDPOINT, CODE_ASSIST_API_VERSION, method)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    # ------------------------------------------------------------------
    # Onboarding / project resolution
    # ------------------------------------------------------------------
    def load_code_assist(self, metadata=None):
        payload = {"metadata": metadata or DEFAULT_METADATA}
        if self._project:
            payload["cloudaicompanionProject"] = self._project
        out = self._post_json(
            self._method_url("loadCodeAssist"), payload, self._headers())
        proj = out.get("cloudaicompanionProject")
        if proj:
            self._project = proj
        return out

    def ensure_project(self, metadata=None):
        """Return the Code Assist project, onboarding the user if necessary."""
        if self._project:
            return self._project
        meta = metadata or DEFAULT_METADATA
        load = self.load_code_assist(meta)
        if self._project:
            return self._project
        tiers = load.get("allowedTiers") or []
        tier_id = next((t.get("id") for t in tiers if t.get("isDefault")),
                       (tiers[0].get("id") if tiers else "free-tier"))
        payload = {"tierId": tier_id, "metadata": meta}
        op = {}
        for _ in range(12):
            op = self._post_json(
                self._method_url("onboardUser"), payload, self._headers())
            if op.get("done"):
                break
            time.sleep(2)
        resp = op.get("response") or {}
        proj = resp.get("cloudaicompanionProject")
        if isinstance(proj, dict):
            proj = proj.get("id")
        self._project = proj
        return self._project

    @property
    def project(self):
        return self._project

    # ------------------------------------------------------------------
    # Streaming generateContent (SSE) with function-calling
    # ------------------------------------------------------------------
    def stream_generate(self, model, contents, *, system_instruction=None,
                        tools=None, generation_config=None, user_prompt_id=""):
        """Yield parsed GenerateContentResponse dicts as they stream in.

        ``contents`` is a Vertex-style list of ``{"role","parts"}``. ``tools`` is
        a list like ``[{"functionDeclarations":[...]}]``. Each yielded item is the
        inner ``response`` object (``{"candidates":[...],"usageMetadata":...}``)."""
        request = {"contents": contents}
        if system_instruction:
            request["systemInstruction"] = {
                "parts": [{"text": system_instruction}]}
        if tools:
            request["tools"] = tools
        if generation_config:
            request["generationConfig"] = generation_config
        payload = {
            "model": model,
            "project": self._project,
            "user_prompt_id": user_prompt_id or "",
            "request": request,
        }
        url = self._method_url("streamGenerateContent", {"alt": "sse"})
        # default=str: functionResponse parts may carry datetime/Decimal/bytes
        # from Odoo .read() results — serialize them as strings rather than raise.
        body = json.dumps(payload, default=str).encode()
        resp = self._request(
            url, data=body,
            headers={"Content-Type": "application/json", **self._headers()},
            stream=True)
        with resp:
            for obj in self._iter_sse(resp):
                # Code Assist wraps the Vertex response under "response".
                yield obj.get("response", obj)

    @staticmethod
    def _iter_sse(resp):
        """Parse an SSE byte stream into JSON objects (one per ``data:`` event)."""
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

    # ------------------------------------------------------------------
    # Response helpers (pure)
    # ------------------------------------------------------------------
    @staticmethod
    def iter_parts(response):
        """Yield the parts of the first candidate of one streamed response."""
        cands = response.get("candidates") or []
        if not cands:
            return
        content = cands[0].get("content") or {}
        for part in content.get("parts") or []:
            yield part

    @staticmethod
    def finish_reason(response):
        cands = response.get("candidates") or []
        return cands[0].get("finishReason") if cands else None
