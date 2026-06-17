# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import json

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..models.aidoo_session import PROVIDERS

_PROVIDER_CODES = {code for code, _label in PROVIDERS}


class AiAssistantChat(http.Controller):

    def _session(self, session_id):
        """Fetch a session owned by the current user (record rule enforced)."""
        session = request.env["aidoo.session"].browse(int(session_id))
        if not session.exists() or session.user_id != request.env.user:
            raise UserError("Session not found.")
        return session

    def _adapter(self, provider):
        if provider not in _PROVIDER_CODES:
            raise UserError("Unknown AI provider.")
        return request.env.user._ai_provider(provider)

    # ------------------------------------------------------------------
    # Provider catalogue (drives the picker + auth panel)
    # ------------------------------------------------------------------
    @http.route("/aidoo/providers", type="json", auth="user")
    def providers(self):
        user = request.env.user
        out = []
        for code, _label in PROVIDERS:
            desc = user._ai_provider(code)._descriptor()
            desc["authenticated"] = user._ai_is_authenticated(code)
            out.append(desc)
        return {"providers": out,
                "default": request.env["aidoo.session"]._default_provider()}

    # ------------------------------------------------------------------
    # Per-user, per-provider authentication
    # ------------------------------------------------------------------
    @http.route("/aidoo/auth/status", type="json", auth="user")
    def auth_status(self, provider=None):
        return self._adapter(provider)._auth_status(request.env.user)

    @http.route("/aidoo/auth/start", type="json", auth="user")
    def auth_start(self, provider=None):
        """oauth_code: returns {url}. device_poll: returns {url, code}."""
        return self._adapter(provider)._auth_start(request.env.user)

    @http.route("/aidoo/auth/complete", type="json", auth="user")
    def auth_complete(self, provider=None, code=None):
        return self._adapter(provider)._auth_complete(request.env.user, code)

    @http.route("/aidoo/auth/import", type="json", auth="user")
    def auth_import(self, provider=None, creds=None):
        return self._adapter(provider)._auth_import(request.env.user, creds)

    @http.route("/aidoo/auth/key", type="json", auth="user")
    def auth_key(self, provider=None, key=None):
        return self._adapter(provider)._auth_set_key(request.env.user, key)

    @http.route("/aidoo/auth/logout", type="json", auth="user")
    def auth_logout(self, provider=None):
        return self._adapter(provider)._auth_logout(request.env.user)

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------
    @http.route("/aidoo/sessions", type="json", auth="user")
    def sessions(self):
        recs = request.env["aidoo.session"].search(
            [("user_id", "=", request.env.uid)], limit=50)
        return [{"id": s.id, "name": s.name, "state": s.state,
                 "provider": s.provider} for s in recs]

    @http.route("/aidoo/new", type="json", auth="user")
    def new(self, provider=None):
        vals = {"user_id": request.env.uid}
        if provider in _PROVIDER_CODES:
            vals["provider"] = provider
        session = request.env["aidoo.session"].create(vals)
        return {"id": session.id, "name": session.name, "state": session.state,
                "provider": session.provider}

    @http.route("/aidoo/messages", type="json", auth="user")
    def messages(self, session_id=None):
        session = self._session(session_id)
        return {
            "id": session.id,
            "name": session.name,
            "state": session.state,
            "provider": session.provider,
            "messages": session.message_ids._to_frontend(),
        }

    @http.route("/aidoo/upload", type="http", auth="user", methods=["POST"])
    def upload(self, session_id=None, ufile=None, **kw):
        """Accept one or more uploaded files for a session (multipart form)."""
        headers = [("Content-Type", "application/json")]
        try:
            session = self._session(session_id)
            files = request.httprequest.files.getlist("ufile")
            if not files:
                raise UserError("No file received.")
            out = [session._store_upload(f.filename, f.read()) for f in files]
            return request.make_response(json.dumps({"attachments": out}), headers)
        except UserError as e:
            return request.make_response(
                json.dumps({"error": str(e)}), headers, status=400)

    @http.route("/aidoo/send", type="json", auth="user")
    def send(self, session_id=None, body=None, attachment_ids=None):
        session = self._session(session_id)
        return session.send_message(body, attachment_ids=attachment_ids)

    @http.route("/aidoo/stop", type="json", auth="user")
    def stop(self, session_id=None):
        session = self._session(session_id)
        session.action_stop()
        return {"state": session.state}
