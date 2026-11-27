# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Antigravity provider — NATIVE (in-process) adapter, EXPERIMENTAL / opt-in.

Antigravity's Google-login ("subscription") backend is the same Code Assist
``v1internal`` API the native Gemini adapter talks to, so this adapter inherits
``aidoo.provider.gemini`` wholesale (streaming, the in-process tool loop through
``aidoo.tool.exec``, contents/schema assembly) and only swaps the client
(``antigravity_client.AntigravityClient``: OAuth client, hosts, headers, request
envelope), the metadata and the login. No ``agy`` binary is needed, and there
are no built-in web/shell tools: the model only gets the user's gated Odoo tools.

Login is a paste-back PKCE flow (like Claude's): the user approves in Google,
the browser lands on Antigravity's ``localhost`` redirect (which does not load),
and the user pastes that URL into the chat. The tokens are stored as
``<home>/.antigravity/oauth_creds.json`` (0600) in the gemini-cli shape, so the
inherited refresh logic works unchanged, and mirrored across cluster nodes via
``aidoo.credential``. The managed Code Assist project is always auto-resolved.

Still opt-in (``aidoo.antigravity.enabled``): it uses Antigravity's OAuth client
outside the official app, which may breach Google's terms and has led to account
restrictions. The previous ``agy`` CLI adapter is kept, unregistered, in
``antigravity_cli.py``.
"""
import json
import logging
import os
import secrets

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider
from .antigravity_client import (
    AntigravityClient, build_auth_url, exchange_code, parse_pasted)
from .codeassist import CodeAssistError

_logger = logging.getLogger(__name__)

_PKCE_PARAM = "aidoo.antigravity.oauth_pkce.%s"


class AidooProviderAntigravity(AidooProvider):
    _name = "aidoo.provider.antigravity"
    _inherit = "aidoo.provider.gemini"
    _description = "Aidoo Provider — Antigravity (native, Google login)"

    _provider_code = "antigravity"
    _provider_label = "Antigravity"
    _accent_color = "#4285F4"
    _provider_icon = "/aidoo/static/src/svg/antigravity-color.svg"
    _config_dir_name = ".antigravity"
    _credential_files = (
        ".antigravity/oauth_creds.json",
        ".antigravity/code_assist_project",
    )
    _default_cli_glob = ""
    _default_model = "gemini-3-flash"
    _default_fallback_model = "gemini-2.5-flash"
    _auth_ui_mode = "oauth_code"
    _auth_key_supported = False
    _auth_code_help = (
        "Google will send you to http://localhost:51121/oauth-callback?… and "
        "the browser will say \"This site can't be reached\". That is "
        "expected. Copy the whole URL from the address bar and paste it below. "
        "Use the most recent login tab only.")
    _auth_code_placeholder = "http://localhost:51121/oauth-callback?state=…&code=…"
    _is_native = True
    # Opt-in: hidden from the picker unless aidoo.antigravity.enabled is set
    # (Google terms / account risk — see the module docstring and SECURITY.md).
    _experimental = True

    def _descriptor(self):
        d = super()._descriptor()
        d["auth_code_help"] = self._auth_code_help
        d["auth_code_placeholder"] = self._auth_code_placeholder
        return d

    # ------------------------------------------------------------------
    # Hooks used by the inherited Gemini _run_stream
    # ------------------------------------------------------------------
    def _new_client(self, creds, **kw):
        return AntigravityClient(creds, **kw)

    def _client_metadata(self):
        return AntigravityClient.METADATA

    def _configured_project(self, user):
        # Antigravity's managed project is always auto-resolved (loadCodeAssist /
        # onboardUser) and cached per user; a user's Gemini Enterprise project
        # does not apply here.
        return ""

    def _api_key(self, user):
        return ""

    def _no_creds_message(self):
        return _("Log in to Antigravity with your Google account to chat.")

    # ------------------------------------------------------------------
    # Authentication — paste-back Google login (PKCE)
    # ------------------------------------------------------------------
    def _auth_start(self, user):
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        self.env["ir.config_parameter"].sudo().set_param(
            _PKCE_PARAM % user.id, json.dumps({"verifier": verifier, "state": state}))
        return {"url": build_auth_url(verifier, state)}

    def _auth_complete(self, user, pasted):
        if not (pasted or "").strip():
            raise UserError(_("Paste the URL your browser landed on after "
                              "approving the Google login."))
        ICP = self.env["ir.config_parameter"].sudo()
        raw = ICP.get_param(_PKCE_PARAM % user.id)
        if not raw:
            raise UserError(_("No login in progress. Click “Login with "
                              "Antigravity” first."))
        pkce = json.loads(raw)
        try:
            code, _state = parse_pasted(pasted, expected_state=pkce.get("state"))
        except ValueError as e:
            if str(e) == "state mismatch":
                raise UserError(_("This URL belongs to an older login attempt. "
                                  "Use the tab from your latest “Login with "
                                  "Antigravity” click, or click Login again."))
            raise UserError(_("Could not read an authorization code from what "
                              "was pasted (%s). Paste the whole address-bar URL.")
                            % e)
        # An authorization code is single-use: whatever happens next, this login
        # attempt is spent.
        ICP.set_param(_PKCE_PARAM % user.id, "")
        try:
            creds = exchange_code(code, pkce["verifier"])
        except CodeAssistError as e:
            _logger.warning("Antigravity OAuth exchange failed for user %s: %s",
                            user.id, e)
            raise UserError(_("Google rejected the login (%s). The code may have "
                              "expired — please log in again.") % e)
        # Confirm the subscription works (and resolve the managed project) before
        # storing anything, so a failed login leaves no half-written credentials.
        client = AntigravityClient(creds)
        try:
            project = client.ensure_project(AntigravityClient.METADATA)
        except CodeAssistError as e:
            _logger.warning("Antigravity project resolution failed for user %s: %s",
                            user.id, e)
            raise UserError(_("Google login worked, but this account could not "
                              "be set up for Antigravity: %s") % e)
        self._write_user_file(
            user, self._oauth_creds_path(user), json.dumps(creds))
        if project:
            self._write_user_file(user, self._project_path(user), project)
        return {"authenticated": True}

    def _auth_import(self, user, blob):
        raise UserError(_("Antigravity connects with Google login; no file or "
                          "API key is needed."))

    def _auth_set_key(self, user, key):
        raise UserError(_("Antigravity connects with Google login; no file or "
                          "API key is needed."))

    def _auth_logout(self, user):
        for path in (self._oauth_creds_path(user), self._project_path(user)):
            try:
                os.remove(path)
            except OSError:
                pass
        return {"authenticated": False}
