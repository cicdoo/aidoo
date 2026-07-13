# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import json

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.tools import consteq

from ..models.aidoo_message import ARTIFACT_MARKER
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
    @http.route("/aidoo/providers", type="jsonrpc", auth="user")
    def providers(self):
        user = request.env.user
        # Ensure the Discuss bot partner exists so users can DM it. Done from a
        # runtime request (full registry) rather than module data, which races
        # the load order of other apps' required res.partner fields.
        request.env["aidoo.session"].sudo()._aidoo_bot_partner()
        out = []
        for code, _label in PROVIDERS:
            adapter = user._ai_provider(code)
            # Experimental providers (e.g. Antigravity, whose web tools can't be
            # sandboxed) stay hidden until explicitly enabled by an admin.
            if getattr(adapter, "_experimental", False) and not adapter._pconfig("enabled"):
                continue
            desc = adapter._descriptor()
            desc["authenticated"] = user._ai_is_authenticated(code)
            out.append(desc)
        # Skills the caller may use (own + shared) — record-rule filtered.
        skills = request.env["aidoo.skill"].search([])
        return {"providers": out,
                "default": request.env["aidoo.session"]._default_provider(),
                "skills": [{"id": s.id, "name": s.name,
                            "description": s.description or ""} for s in skills]}

    # ------------------------------------------------------------------
    # Per-user, per-provider authentication
    # ------------------------------------------------------------------
    @http.route("/aidoo/auth/status", type="jsonrpc", auth="user")
    def auth_status(self, provider=None):
        return self._adapter(provider)._auth_status(request.env.user)

    @http.route("/aidoo/auth/start", type="jsonrpc", auth="user")
    def auth_start(self, provider=None):
        """oauth_code: returns {url}. device_poll: returns {url, code}."""
        return self._adapter(provider)._auth_start(request.env.user)

    @http.route("/aidoo/auth/complete", type="jsonrpc", auth="user")
    def auth_complete(self, provider=None, code=None):
        return self._adapter(provider)._auth_complete(request.env.user, code)

    @http.route("/aidoo/auth/import", type="jsonrpc", auth="user")
    def auth_import(self, provider=None, creds=None):
        return self._adapter(provider)._auth_import(request.env.user, creds)

    @http.route("/aidoo/auth/key", type="jsonrpc", auth="user")
    def auth_key(self, provider=None, key=None):
        return self._adapter(provider)._auth_set_key(request.env.user, key)

    @http.route("/aidoo/auth/logout", type="jsonrpc", auth="user")
    def auth_logout(self, provider=None):
        return self._adapter(provider)._auth_logout(request.env.user)

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------
    @http.route("/aidoo/sessions", type="jsonrpc", auth="user")
    def sessions(self):
        # Clean up empty conversations left behind by chats that were opened but
        # never used (e.g. the floating bubble opened and closed without typing).
        request.env["aidoo.session"]._prune_empty()
        recs = request.env["aidoo.session"].search(
            [("user_id", "=", request.env.uid)], limit=50)
        return [{"id": s.id, "name": s.name, "state": s.state,
                 "provider": s.provider, "is_favorite": s.is_favorite}
                for s in recs]

    @http.route("/aidoo/favorite", type="jsonrpc", auth="user")
    def favorite(self, session_id=None, is_favorite=None):
        session = self._session(session_id)
        session.is_favorite = bool(is_favorite)
        return {"id": session.id, "is_favorite": session.is_favorite}

    def _readable_skills(self, skill_ids):
        """Return the subset of ``skill_ids`` the caller may use. Resolved
        through search so the record rules (own + shared) apply — a private
        skill of another user is simply not found."""
        ids = [int(i) for i in (skill_ids or []) if i]
        if not ids:
            return request.env["aidoo.skill"]
        return request.env["aidoo.skill"].search([("id", "in", ids)])

    @http.route("/aidoo/skill", type="jsonrpc", auth="user")
    def set_skill(self, session_id=None, skill_ids=None, skill_id=None):
        """Set (or clear) the instruction skills on a conversation. Takes
        effect on the next turn. ``skill_id`` is a legacy single-id alias."""
        session = self._session(session_id)
        if skill_ids is None and skill_id is not None:
            skill_ids = [skill_id] if skill_id else []
        session.skill_ids = [(6, 0, self._readable_skills(skill_ids).ids)]
        return {"id": session.id, "skill_ids": session.skill_ids.ids}

    @http.route("/aidoo/discard", type="jsonrpc", auth="user")
    def discard(self, session_id=None):
        """Delete a session only if it is empty (no messages, not running).
        Used when a chat surface is closed without any interaction."""
        session = self._session(session_id)
        if not session.message_ids and session.state != "running":
            session.unlink()
            return {"deleted": True}
        return {"deleted": False}

    @http.route("/aidoo/delete", type="jsonrpc", auth="user")
    def delete(self, session_id=None):
        """Delete one of the caller's own conversations and all its messages.

        Ownership is enforced by ``_session`` (record-rule scoped). Refuses while
        a run is in flight so we never unlink a session a background worker is
        still writing to. The messages cascade with the session."""
        session = self._session(session_id)
        if session.state == "running":
            return {"deleted": False, "reason": "running"}
        session.unlink()
        return {"deleted": True}

    @http.route("/aidoo/new", type="jsonrpc", auth="user")
    def new(self, provider=None, skill_ids=None, skill_id=None,
            context_model=None,
            context_res_id=None, context_domain=None, context_view_type=None,
            context_groupby=None, context_filters=None, context_selected_ids=None):
        vals = {"user_id": request.env.uid}
        if provider in _PROVIDER_CODES:
            vals["provider"] = provider
        if skill_ids is None and skill_id is not None:
            skill_ids = [skill_id] if skill_id else []
        skills = self._readable_skills(skill_ids)
        if skills:
            vals["skill_ids"] = [(6, 0, skills.ids)]
        # Optional focus context (from "Ask Aidoo"): a record on a form, or the
        # filtered/selected record set on any multi-record view. Only stored when
        # the user may actually read the model — never seed something outside
        # their rights (the records are still re-read under their own rights at
        # tool time, so the ids/domain leak nothing on their own).
        if context_model and context_model in request.env:
            try:
                Model = request.env[context_model]
                Model.check_access("read")
            except AccessError:
                Model = None
            if Model is not None:
                vals["context_model"] = context_model
                if context_res_id:
                    try:
                        rec = Model.browse(int(context_res_id))
                        rec.check_access("read")
                        if rec.exists():
                            vals["context_res_id"] = rec.id
                    except (AccessError, ValueError):
                        pass
                else:
                    # A multi-record view: keep the domain, group-by, filter
                    # summary, view type and explicit selection.
                    if context_domain:
                        try:
                            dom = json.loads(context_domain)
                            if isinstance(dom, list):
                                vals["context_domain"] = json.dumps(dom)
                        except (ValueError, TypeError):
                            pass
                    if context_view_type:
                        vals["context_view_type"] = str(context_view_type)[:32]
                    if context_filters:
                        vals["context_filters"] = str(context_filters)[:1000]
                    if context_groupby:
                        try:
                            gb = json.loads(context_groupby)
                            if isinstance(gb, list):
                                vals["context_groupby"] = json.dumps(
                                    [str(g) for g in gb][:20])
                        except (ValueError, TypeError):
                            pass
                    if context_selected_ids:
                        try:
                            ids = json.loads(context_selected_ids)
                            if isinstance(ids, list):
                                ids = [int(i) for i in ids if isinstance(i, (int, float))][:200]
                                if ids:
                                    vals["context_selected_ids"] = json.dumps(ids)
                        except (ValueError, TypeError):
                            pass
        session = request.env["aidoo.session"].create(vals)
        return {"id": session.id, "name": session.name, "state": session.state,
                "provider": session.provider,
                "is_favorite": session.is_favorite,
                "skill_ids": session.skill_ids.ids,
                "context_label": session._context_label()}

    @http.route("/aidoo/messages", type="jsonrpc", auth="user")
    def messages(self, session_id=None, limit=None, before_id=None):
        """Conversation header + a page of messages.

        Without ``limit`` the whole history is returned (legacy). With ``limit``
        the newest page is returned, and ``before_id`` pages further back (the
        client requests older messages as the user scrolls up). ``has_more``
        signals whether still-older messages remain.
        """
        session = self._session(session_id)
        page = session._messages_page(limit=limit, before_id=before_id)
        return {
            "id": session.id,
            "name": session.name,
            "state": session.state,
            "provider": session.provider,
            "skill_ids": session.skill_ids.ids,
            "context_label": session._context_label(),
            "messages": page["messages"],
            "has_more": page["has_more"],
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

    # ------------------------------------------------------------------
    # Generated HTML artifacts — served inline (never a generic attachment
    # reader: only session-linked attachments carrying the artifact marker).
    # ------------------------------------------------------------------
    def _artifact(self, attachment_id):
        """Sudo-fetch an artifact attachment; None when the id is not an
        aidoo artifact (indistinguishable from missing, on purpose)."""
        try:
            attachment_id = int(attachment_id)
        except (TypeError, ValueError):
            return None
        att = request.env["ir.attachment"].sudo().browse(
            attachment_id).exists()
        if (not att or att.res_model != "aidoo.session"
                or att.description != ARTIFACT_MARKER):
            return None
        return att

    def _own_artifact(self, attachment_id):
        """An artifact whose session the current user owns; UserError otherwise
        (same non-leaking style as ``_session``)."""
        att = self._artifact(attachment_id or 0)
        session = att and request.env["aidoo.session"].sudo().browse(att.res_id)
        if not att or session.user_id != request.env.user:
            raise UserError("Artifact not found.")
        return att

    @http.route("/aidoo/artifact/<int:attachment_id>", type="http",
                auth="public", methods=["GET"])
    def artifact(self, attachment_id, token=None, **kw):
        """Serve a generated HTML artifact inline.

        Access: a valid share token (external), or a logged-in internal user who
        owns the session or belongs to its Discuss channel. The response CSP
        sandboxes the document into an opaque origin with no script execution;
        the only network subresources allowed are same-origin images/fonts
        (e.g. /logo.png) — parity with the chat's srcdoc iframe.
        """
        att = self._artifact(attachment_id)
        if not att:
            return request.not_found()
        if token:
            # Odoo 19 dropped ir.attachment.validate_access; gate the public
            # share link by constant-time comparing the query token against the
            # attachment's own access_token (att is already sudo-fetched).
            if not att.access_token or not consteq(token, att.access_token):
                return request.not_found()
        else:
            user = request.env.user
            if not user._is_internal():
                return request.not_found()
            session = request.env["aidoo.session"].sudo().browse(att.res_id)
            allowed = session.user_id == user or (
                session.discuss_channel_id
                and user.partner_id in session.discuss_channel_id.channel_partner_ids)
            if not allowed:
                return request.not_found()
        return request.make_response(att.raw or b"", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Security-Policy",
             "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
             "img-src 'self' data:; font-src 'self' data:"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Disposition", "inline"),
            ("Cache-Control", "private, no-store"),
        ])

    @http.route("/aidoo/artifact/share", type="jsonrpc", auth="user")
    def artifact_share(self, attachment_id=None):
        """Mint (or return) the public share link. Session owner only."""
        att = self._own_artifact(attachment_id)
        token = att.generate_access_token()[0]
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return {"url": "%s/aidoo/artifact/%d?token=%s" % (base, att.id, token),
                "shared": True}

    @http.route("/aidoo/artifact/revoke", type="jsonrpc", auth="user")
    def artifact_revoke(self, attachment_id=None):
        """Kill the public share link. Session owner only."""
        att = self._own_artifact(attachment_id)
        att.access_token = False
        return {"shared": False}

    @http.route("/aidoo/send", type="jsonrpc", auth="user")
    def send(self, session_id=None, body=None, attachment_ids=None):
        session = self._session(session_id)
        return session.send_message(body, attachment_ids=attachment_ids)

    @http.route("/aidoo/stop", type="jsonrpc", auth="user")
    def stop(self, session_id=None):
        session = self._session(session_id)
        session.action_stop()
        return {"state": session.state}
