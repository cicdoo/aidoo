# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Dependency-free client for Google **Antigravity**'s subscription backend.

Antigravity's Google-login ("subscription") traffic goes to the same Code Assist
``v1internal`` API the Gemini client speaks (``codeassist.py``): loadCodeAssist /
onboardUser to resolve the managed project, then ``streamGenerateContent`` (SSE)
with function-calling. What differs is only *who the client says it is*: another
public OAuth client, the Antigravity hosts, a User-Agent, ``ideType`` metadata and
an extra request envelope (``userAgent`` / ``requestType`` / ``requestId``).
So this is a thin subclass of :class:`CodeAssistClient` plus the PKCE login
helpers. Like ``codeassist.py`` it is Odoo-free (stdlib only).

Protocol constants follow the public reverse-engineered implementation
https://github.com/NoeFabris/opencode-antigravity-auth (``src/constants.ts``,
``src/antigravity/oauth.ts``, ``src/plugin/project.ts``, ``src/plugin/request.ts``,
``main`` as of 2026-11-27; itself ported from CLIProxyAPI). They are not an
official, documented Google API and may change without notice.
"""
import base64
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .codeassist import OAUTH_TOKEN_URL, CodeAssistClient, CodeAssistError

# The Antigravity installed-app OAuth client. Public in the same sense as the
# gemini-cli client in codeassist.py: it ships inside the desktop app and only
# identifies the application; every token still needs the user's own consent.
OAUTH_CLIENT_ID = ("1071006060591-tmhssin2h21lcre235vtolojh4g403ep"
                   ".apps.googleusercontent.com")
OAUTH_CLIENT_SECRET = "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"
OAUTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# The app's loopback redirect. Nothing listens there for us: the browser shows a
# "site can't be reached" page and the user pastes that URL back into the chat.
OAUTH_REDIRECT_URI = "http://localhost:51121/oauth-callback"
OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)

# Daily host first (what the Antigravity app uses), production as failover.
ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
)
ANTIGRAVITY_VERSION = "2.24.0"
ANTIGRAVITY_CHANGELIST = "1000335682"
USER_AGENT = ("antigravity/hub/%s (aidev_client; os_type=darwin; arch=arm64; cl=%s)"
              % (ANTIGRAVITY_VERSION, ANTIGRAVITY_CHANGELIST))


class AntigravityClient(CodeAssistClient):
    """Code Assist client speaking as Antigravity (see module docstring)."""

    ENDPOINT = ENDPOINTS[0]
    ENDPOINTS = ENDPOINTS
    OAUTH_CLIENT_ID = OAUTH_CLIENT_ID
    OAUTH_CLIENT_SECRET = OAUTH_CLIENT_SECRET
    # loadCodeAssist / onboardUser metadata. The reference sends only ideType
    # (platform/pluginType are omitted on purpose).
    METADATA = {"ideType": "ANTIGRAVITY"}

    def _is_api_url(self, url):
        return any(url.startswith(h) for h in self.ENDPOINTS)

    def _request(self, url, **kw):
        """Fail over once to the next host on a network error, 404 or 5xx.

        429 is a quota answer, not a host problem, so it is raised as-is (the
        caller may switch model). The host that answered is remembered on this
        client so the rest of the turn goes straight to it."""
        current = next((h for h in self.ENDPOINTS if url.startswith(h)), None)
        if current is None:
            return super()._request(url, **kw)
        path = url[len(current):]
        hosts = [current] + [h for h in self.ENDPOINTS if h != current]
        last = None
        for host in hosts[:2]:
            try:
                out = super()._request(host + path, **kw)
            except CodeAssistError as e:
                retryable = e.status is None or e.status == 404 or e.status >= 500
                if not retryable:
                    raise
                last = e
                continue
            self.ENDPOINT = host
            return out
        raise last

    def _extra_headers(self):
        # The reference sends only the User-Agent on Antigravity-style calls (it
        # deliberately strips X-Goog-Api-Client / Client-Metadata).
        return {"User-Agent": USER_AGENT}

    def _wrap_payload(self, model, request, user_prompt_id=""):
        return {
            "project": self._project,
            "model": model,
            "request": request,
            "userAgent": "antigravity",
            "requestType": "agent",
            "requestId": "agent-%s" % uuid.uuid4(),
        }


# ---------------------------------------------------------------------------
# Paste-back PKCE login
# ---------------------------------------------------------------------------
def _challenge(verifier):
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def build_auth_url(verifier, state):
    """Google consent URL for the Antigravity client (PKCE S256, offline)."""
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": " ".join(OAUTH_SCOPES),
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return "%s?%s" % (OAUTH_AUTHORIZE_URL, urllib.parse.urlencode(params))


def parse_pasted(text, expected_state=None):
    """``(code, state)`` from the pasted redirect URL (or a bare code).

    Raises ValueError when nothing usable was pasted, Google returned an error,
    or ``expected_state`` is given and the URL carries a different state."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty")
    if "code=" in text or "error=" in text:
        query = urllib.parse.urlsplit(text).query if "://" in text else text
        query = query.lstrip("?")
        qs = urllib.parse.parse_qs(query)
        if qs.get("error"):
            raise ValueError("Google returned: %s" % qs["error"][0])
        code = (qs.get("code") or [""])[0].strip()
        state = (qs.get("state") or [""])[0].strip() or None
    else:
        code, state = text, None
    if not code:
        raise ValueError("no code")
    if expected_state and state and state != expected_state:
        raise ValueError("state mismatch")
    return code, state


def exchange_code(code, verifier, timeout=30):
    """Trade an authorization code for tokens, in ``oauth_creds.json`` shape
    (``expiry_date`` in ms) so CodeAssistClient's refresh works unchanged."""
    form = urllib.parse.urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=form, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            detail = body.get("error_description") or body.get("error") or ""
        except Exception:  # noqa: BLE001
            pass
        raise CodeAssistError("Token exchange failed (HTTP %s) %s"
                              % (e.code, detail), status=e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CodeAssistError("Token exchange failed: %s" % e)
    if not data.get("access_token"):
        raise CodeAssistError("Token exchange returned no access_token.")
    creds = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or "",
        "expiry_date": int(time.time() * 1000
                           + int(data.get("expires_in") or 3600) * 1000),
        "scope": data.get("scope") or " ".join(OAUTH_SCOPES),
        "token_type": data.get("token_type") or "Bearer",
    }
    if data.get("id_token"):
        creds["id_token"] = data["id_token"]
    return creds
