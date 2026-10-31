# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Grok (xAI) provider — NATIVE (in-process) adapter.

Like the native Gemini, Codex and Z.AI adapters, this provider does **not**
drive a CLI subprocess. xAI's API (``https://api.x.ai/v1``) is OpenAI-compatible
chat completions, so the adapter inherits the Z.AI native loop wholesale
(``zai.py`` + ``zai_client.py``: SSE streaming, retries, in-process tool calls
through ``aidoo.tool.exec``) and only swaps the endpoint, the labels and how the
Bearer token is obtained. The legacy CLI adapter is kept unregistered in
``grok_cli.py``.

Per-user auth (unchanged location/format, under ``<home>/.grok``):

* an imported ``auth.json`` from `grok login` — an xAI OIDC session whose
  access token (``key``) is accepted by api.x.ai as a Bearer token. It lives
  ~6h; we refresh it against the issuer's token endpoint with the stored
  ``refresh_token`` (rotating it) and write it back. Takes precedence.
* or an xAI API key (per user, else the shared ``aidoo.grok.api_key``).
"""
import fcntl
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider

_logger = logging.getLogger(__name__)

XAI_API_BASE = "https://api.x.ai/v1"
XAI_OIDC_ISSUER = "https://auth.x.ai"

# Refresh when the access token expires within 5 minutes (same slack as Codex).
_EXPIRY_SLACK_S = 300


def _parse_expiry(value):
    """``expires_at`` (RFC 3339, e.g. ``2026-10-22T08:24:45.448583Z``) -> epoch
    seconds, or None when missing/unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _format_expiry(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _login_entry(data):
    """The (entry_key, entry) of the OIDC session in a `grok login` auth.json:
    ``{"https://auth.x.ai::<client_id>": {"key": ..., "refresh_token": ...}}``.
    Returns (None, None) when no entry carries an access token."""
    if not isinstance(data, dict):
        return None, None
    for k, v in data.items():
        if isinstance(v, dict) and v.get("key"):
            return k, v
    return None, None


class AidooProviderGrok(AidooProvider):
    _name = "aidoo.provider.grok"
    _inherit = "aidoo.provider.zai"
    _description = "Aidoo Provider — Grok (native xAI API)"

    _provider_code = "grok"
    _provider_label = "Grok"
    _accent_color = "#714b67"
    _provider_icon = "/aidoo/static/src/svg/grok.svg"
    _config_dir_name = ".grok"
    _default_model = "grok-4.7"
    _default_base_url = XAI_API_BASE
    _auth_ui_mode = "import_or_key"
    _auth_key_supported = True
    _auth_import_help = (
        "Run `grok login` on a machine with a browser and paste the contents of "
        "~/.grok/auth.json, or paste an xAI API key "
        "(from https://console.x.ai) in the API-key field below.")
    _uses_system_prompt_flag = False
    _is_native = True

    # ------------------------------------------------------------------
    # Per-user credential files (under the user's .grok config dir)
    # ------------------------------------------------------------------
    def _auth_path(self, user):
        """Per-user ``auth.json`` produced by `grok login` (the OAuth path)."""
        return os.path.join(self._config_dir(user), "auth.json")

    def _load_auth(self, user):
        try:
            with open(self._auth_path(user), "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _has_oauth(self, user):
        """True if the imported login is usable: a live access token, or one we
        can renew with its refresh token."""
        _k, entry = _login_entry(self._load_auth(user))
        if not entry:
            return False
        exp = _parse_expiry(entry.get("expires_at"))
        return bool(entry.get("refresh_token")) or exp is None or exp > time.time()

    def _is_authenticated(self, user):
        return bool(self._has_oauth(user) or self._api_key(user))

    def _not_connected_text(self):
        return _("Connect Grok to chat. Open the connect panel and paste your "
                 "~/.grok/auth.json (from `grok login`) or an xAI API key.")

    def _bearer_token(self, user):
        """The imported login's access token (refreshed when near expiry), else
        the xAI API key. Empty when neither is configured."""
        if self._has_oauth(user):
            return self._oauth_access_token(user)
        return self._api_key(user)

    # ------------------------------------------------------------------
    # OIDC refresh — serialized per user across Odoo workers with a lock file,
    # since xAI rotates the refresh token and a lost race would orphan it.
    # ------------------------------------------------------------------
    def _oauth_access_token(self, user):
        entry_key, entry = _login_entry(self._load_auth(user))
        if not self._needs_refresh(entry):
            return entry["key"]
        lock_path = self._auth_path(user) + ".lock"
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Re-read under the lock: another worker may have refreshed already.
            data = self._load_auth(user)
            entry_key, entry = _login_entry(data)
            if not entry:  # logged out meanwhile
                return self._api_key(user)
            if not self._needs_refresh(entry):
                return entry["key"]
            data[entry_key] = self._refresh_entry(entry)
            try:
                self._write_user_file(
                    user, self._auth_path(user), json.dumps(data, indent=2))
            except Exception:  # noqa: BLE001 — persistence is best-effort
                # The new token still works for this turn, but the rotated
                # refresh token is lost, forcing a re-import once it expires.
                _logger.warning(
                    "Grok: failed to persist refreshed OAuth tokens for user %s",
                    user.id, exc_info=True)
            return data[entry_key]["key"]
        finally:
            os.close(fd)

    @staticmethod
    def _needs_refresh(entry):
        exp = _parse_expiry(entry.get("expires_at"))
        return exp is not None and exp - time.time() < _EXPIRY_SLACK_S

    def _refresh_entry(self, entry):
        """Exchange the entry's refresh token; return the updated entry (all
        unknown fields preserved, the refresh token kept unless rotated)."""
        rt = entry.get("refresh_token")
        if not rt:
            raise UserError(_(
                "Your Grok login has expired. Run `grok login` again and "
                "re-import ~/.grok/auth.json."))
        issuer = (entry.get("oidc_issuer") or XAI_OIDC_ISSUER).rstrip("/")
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": entry.get("oidc_client_id") or "",
        }).encode()
        req = urllib.request.Request(
            issuer + "/oauth2/token", data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(
                    req, context=ssl.create_default_context(), timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            _logger.warning("Grok token refresh failed: HTTP %s %s", e.code, detail)
            raise UserError(_(
                "Grok token refresh failed (HTTP %(code)s). Run `grok login` "
                "again and re-import ~/.grok/auth.json.", code=e.code)) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise UserError(_("Grok token refresh failed: %s") % e) from e
        if not data.get("access_token"):
            raise UserError(_("Grok token refresh returned no access token."))
        new = dict(entry)
        new["key"] = data["access_token"]
        if data.get("refresh_token"):
            new["refresh_token"] = data["refresh_token"]
        new["expires_at"] = _format_expiry(
            time.time() + int(data.get("expires_in") or 3600))
        return new

    # ------------------------------------------------------------------
    # Authentication — import_or_key (auth.json import OR xAI API key)
    # ------------------------------------------------------------------
    def _auth_import(self, user, blob):
        """Validate and store an imported Grok ``auth.json`` blob (the content
        of ``~/.grok/auth.json`` after `grok login`). A pasted bare API key is
        routed to the key store so the import box still works for keys."""
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste your Grok auth.json content."))
        if not blob.startswith("{"):
            return self._auth_set_key(user, blob)
        try:
            data = json.loads(blob)
        except ValueError:
            raise UserError(_("That is not valid JSON. Paste the full content of "
                              "your ~/.grok/auth.json file."))
        if not _login_entry(data)[1]:
            raise UserError(_(
                "The pasted content is not a valid Grok credential file. Run "
                "`grok login` on a machine with a browser, then paste the "
                "contents of ~/.grok/auth.json."))
        self._write_user_file(user, self._auth_path(user), json.dumps(data))
        return {"authenticated": self._is_authenticated(user)}

    def _auth_logout(self, user):
        """Remove this user's stored credentials (forces re-connection)."""
        for path in (self._auth_path(user), self._api_key_path(user)):
            try:
                os.remove(path)
            except OSError:
                pass
        return {"authenticated": False}
