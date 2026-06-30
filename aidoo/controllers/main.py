# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import json

from odoo import http
from odoo.exceptions import AccessError, UserError
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
        # Clean up empty conversations left behind by chats that were opened but
        # never used (e.g. the floating bubble opened and closed without typing).
        request.env["aidoo.session"]._prune_empty()
        recs = request.env["aidoo.session"].search(
            [("user_id", "=", request.env.uid)], limit=50)
        return [{"id": s.id, "name": s.name, "state": s.state,
                 "provider": s.provider, "is_favorite": s.is_favorite}
                for s in recs]

    @http.route("/aidoo/favorite", type="json", auth="user")
    def favorite(self, session_id=None, is_favorite=None):
        session = self._session(session_id)
        session.is_favorite = bool(is_favorite)
        return {"id": session.id, "is_favorite": session.is_favorite}

    def _readable_skill(self, skill_id):
        """Return the skill the caller may use, or an empty recordset. Resolved
        through search so the record rules (own + shared) apply — a private skill
        of another user is simply not found."""
        if not skill_id:
            return request.env["aidoo.skill"]
        return request.env["aidoo.skill"].search(
            [("id", "=", int(skill_id))], limit=1)

    @http.route("/aidoo/skill", type="json", auth="user")
    def set_skill(self, session_id=None, skill_id=None):
        """Attach (or clear) the instruction skill on a conversation. Takes
        effect on the next turn."""
        session = self._session(session_id)
        session.skill_id = self._readable_skill(skill_id).id or False
        return {"id": session.id, "skill_id": session.skill_id.id or False}

    @http.route("/aidoo/discard", type="json", auth="user")
    def discard(self, session_id=None):
        """Delete a session only if it is empty (no messages, not running).
        Used when a chat surface is closed without any interaction."""
        session = self._session(session_id)
        if not session.message_ids and session.state != "running":
            session.unlink()
            return {"deleted": True}
        return {"deleted": False}

    @http.route("/aidoo/delete", type="json", auth="user")
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

    @http.route("/aidoo/new", type="json", auth="user")
    def new(self, provider=None, skill_id=None, context_model=None,
            context_res_id=None, context_domain=None, context_view_type=None,
            context_groupby=None, context_filters=None, context_selected_ids=None):
        vals = {"user_id": request.env.uid}
        if provider in _PROVIDER_CODES:
            vals["provider"] = provider
        skill = self._readable_skill(skill_id)
        if skill:
            vals["skill_id"] = skill.id
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
                "skill_id": session.skill_id.id or False,
                "context_label": session._context_label()}

    @http.route("/aidoo/messages", type="json", auth="user")
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
            "skill_id": session.skill_id.id or False,
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

    @http.route("/aidoo/send", type="json", auth="user")
    def send(self, session_id=None, body=None, attachment_ids=None):
        session = self._session(session_id)
        return session.send_message(body, attachment_ids=attachment_ids)

    @http.route("/aidoo/stop", type="json", auth="user")
    def stop(self, session_id=None):
        session = self._session(session_id)
        session.action_stop()
        return {"state": session.state}
