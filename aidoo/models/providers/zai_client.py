# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Dependency-free client for z.ai's GLM **Coding Plan** chat API.

z.ai exposes an OpenAI-compatible chat-completions endpoint under the coding
namespace — ``https://api.z.ai/api/coding/paas/v4/chat/completions`` — billed
against the GLM Coding Plan subscription. The native z.ai provider talks to it
directly from Python (stdlib ``urllib`` + ``ssl`` only, mirroring the native
Gemini adapter's ``codeassist`` client) and runs the agent tool-calling loop
in-process, so no CLI subprocess is spawned.

The client is intentionally Odoo-free so it can be unit-tested standalone. It is
much simpler than the Code Assist client: a static Bearer API key (no OAuth
refresh, no onboarding/project resolution), and OpenAI-shaped requests/responses.
"""
import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

# GLM Coding Plan endpoint (OpenAI-compatible). NOT /api/anthropic and NOT the
# general /api/paas/v4 — this is the coding-subscription base.
ZAI_CODING_BASE = "https://api.z.ai/api/coding/paas/v4"


# ---------------------------------------------------------------------------
# Proactive client-side rate limiting (token bucket), shared per quota key
# across all runs in THIS Odoo worker process. Not coordinated across worker
# processes — each process self-limits to the configured RPM.
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


class ZaiError(Exception):
    """Raised for any non-2xx / transport failure talking to z.ai.

    ``status`` is the HTTP status when known; ``is_rate_limit`` is True for 429
    so callers can branch (e.g. surface a retry notice)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status
        self.is_rate_limit = status == 429


class ZaiClient:
    """Talks to z.ai's coding chat-completions endpoint with a Bearer API key."""

    _MAX_ATTEMPTS = 6           # total tries (>= 2)
    _INITIAL_DELAY_S = 5
    _MAX_DELAY_S = 30           # cap for the exponential fallback
    _HINT_CAP_S = 90            # absolute cap when honoring a server reset hint
    _MAX_NET_RETRIES = 2        # transient network blips only

    def __init__(self, api_key, base_url=ZAI_CODING_BASE, timeout=120,
                 on_retry=None, rpm=0, deadline=None):
        self._api_key = (api_key or "").strip()
        self._base = (base_url or ZAI_CODING_BASE).rstrip("/")
        self._timeout = timeout
        # Optional callback(attempt:int, delay_s:float, reason) invoked before
        # each backoff wait, so the caller can surface a "retrying in Ns" notice.
        self._on_retry = on_retry
        self._rpm = int(rpm or 0)
        # Optional wall-clock deadline (epoch seconds, as from ``time.time()``):
        # the caller's turn timeout. We refuse to start a backoff wait that would
        # run past it, so retries can never block the run beyond its deadline.
        self._deadline = deadline
        # Quota key for the proactive limiter: scope to this key, not global, so
        # distinct keys don't starve each other. Hash to keep the secret out of
        # any in-memory map key that might be logged.
        self._quota_key = str(hash(self._api_key))
        self._ctx = ssl.create_default_context()

    def _headers(self):
        return {"Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json"}

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
                raise ZaiError(
                    "HTTP %s for %s: %s" % (e.code, url, body), status=e.code)
            except (urllib.error.URLError, OSError) as e:
                if attempt < self._MAX_NET_RETRIES:
                    delay = self._backoff(attempt)
                    if self._can_wait(delay):
                        self._notify_retry(attempt + 1, delay, "network")
                        time.sleep(delay)
                        attempt += 1
                        continue
                raise ZaiError("Network error for %s: %s" % (url, e))
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
        "reset after Ns" hint -> exponential backoff."""
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
        m = re.search(r"reset(?:s)? (?:after|in) (\d+)\s*s", body or "")
        if m:
            return min(int(m.group(1)) + 1, cls._HINT_CAP_S)
        return cls._backoff(attempt)

    # ------------------------------------------------------------------
    # Streaming chat completions (SSE) with OpenAI-style function calling
    # ------------------------------------------------------------------
    def stream_chat(self, model, messages, *, tools=None, temperature=None):
        """POST /chat/completions with stream:true; yield parsed SSE chunk dicts.

        ``messages`` is the OpenAI message list; ``tools`` a list of
        ``{"type":"function","function":{...}}``. Each yielded item is a parsed
        chunk: ``{"choices":[{"delta":{...},"finish_reason":...}], ...}``."""
        payload = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if temperature is not None:
            payload["temperature"] = temperature
        url = self._base + "/chat/completions"
        # default=str: tool results fed back as message content may carry
        # datetime/Decimal/bytes from Odoo .read() — serialize as strings.
        body = json.dumps(payload, default=str).encode()
        resp = self._request(url, data=body, headers=self._headers(), stream=True)
        with resp:
            for obj in self._iter_sse(resp):
                yield obj

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
    # Tool-call accumulation (pure)
    # ------------------------------------------------------------------
    @staticmethod
    def accumulate_tool_calls(acc, delta_tool_calls):
        """Merge a streamed ``delta.tool_calls`` list into ``acc`` (keyed by the
        OpenAI ``index``). Each fragment may carry an ``id``, ``function.name``
        and a streamed ``function.arguments`` string chunk; we concatenate the
        argument fragments and keep the first id/name seen for each index.

        Also tolerates a non-streamed final form where the whole tool_call (id +
        name + complete arguments) arrives in one fragment."""
        for tc in delta_tool_calls or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            slot = acc.setdefault(idx, {"id": None, "name": None, "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
        return acc
