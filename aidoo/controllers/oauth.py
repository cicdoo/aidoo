# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""OAuth 2.1 authorization server for the remote MCP endpoint.

Implements what ChatGPT / Claude custom connectors need to connect without a
hand-made API key: RFC 9728 protected-resource metadata, RFC 8414
authorization-server metadata, RFC 7591 dynamic client registration and the
authorization-code + PKCE (S256) flow with rotating refresh tokens. Access
tokens are Odoo API keys of scope ``aidoo_mcp`` (see ``aidoo.oauth.grant``).
All routes follow the ``aidoo.mcp_enabled`` switch.
"""
import json
import logging
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from odoo import http
from odoo.http import request

from ..models.aidoo_oauth import MAX_UNUSED_CLIENTS, SCOPE
from .mcp import _enabled

_logger = logging.getLogger(__name__)

AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
LOOPBACK = ("localhost", "127.0.0.1", "::1")
NO_STORE = [("Cache-Control", "no-store"), ("Pragma", "no-cache")]


def _json(data, status=200, headers=None):
    return request.make_json_response(data, status=status, headers=headers)


def _base():
    return request.httprequest.host_url.rstrip("/")


# The issuer lives under /aidoo, so its metadata sits at path-scoped
# well-known URLs (RFC 8414 section 3.1, RFC 9728 section 3.1). The bare
# /.well-known/* URLs belong to whatever else on the host speaks OAuth (another
# module's MCP server, for one): claiming them here would send that server's
# clients to the wrong authorization server.
ISSUER_PATH = "/aidoo"


def _issuer():
    return _base() + ISSUER_PATH


def _disabled():
    icp = request.env["ir.config_parameter"].sudo()
    if _enabled(icp):
        return None
    return _json({"error": "MCP server disabled",
                  "hint": "Enable it in Settings → Aidoo"}, status=403)


def _valid_redirect_uri(uri):
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return False
    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError:
        return False
    if parts.fragment or "#" in uri or parts.username or parts.password or not host:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in LOOPBACK


def _redirect_with(uri, params):
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in params.items() if v is not None]
    return request.redirect(
        urlunsplit(parts._replace(query=urlencode(query))), code=302, local=False)


class AidooOauth(http.Controller):

    # --- discovery ------------------------------------------------------
    @http.route("/.well-known/oauth-protected-resource/aidoo/mcp",
                type="http", auth="none", methods=["GET"], cors="*", csrf=False)
    def protected_resource(self, **kw):
        if (off := _disabled()):
            return off
        base = _base()
        return _json({
            "resource": base + "/aidoo/mcp",
            "authorization_servers": [_issuer()],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [SCOPE],
        })

    @http.route(["/.well-known/oauth-authorization-server/aidoo",
                 "/aidoo/.well-known/oauth-authorization-server"],
                type="http", auth="none", methods=["GET"], cors="*", csrf=False)
    def authorization_server(self, **kw):
        if (off := _disabled()):
            return off
        base = _base()
        return _json({
            "issuer": _issuer(),
            "authorization_endpoint": base + "/aidoo/oauth/authorize",
            "token_endpoint": base + "/aidoo/oauth/token",
            "registration_endpoint": base + "/aidoo/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "scopes_supported": [SCOPE],
        })

    # --- dynamic client registration (RFC 7591) -------------------------
    @http.route("/aidoo/oauth/register", type="http", auth="none",
                methods=["POST"], cors="*", csrf=False, readonly=False)
    def register(self, **kw):
        if (off := _disabled()):
            return off

        def bad(msg, code="invalid_client_metadata"):
            return _json({"error": code, "error_description": msg}, status=400)

        raw = request.httprequest.get_data(cache=False)
        if len(raw) > 16 * 1024:
            return bad("Request too large", "invalid_request")
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return bad("Body must be JSON", "invalid_request")
        if not isinstance(body, dict):
            return bad("Body must be a JSON object", "invalid_request")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 10 \
                or not all(_valid_redirect_uri(u) for u in uris):
            return bad("redirect_uris must be a list of https (or loopback "
                       "http) URIs without fragments")
        name = body.get("client_name") or "MCP client"
        if not isinstance(name, str) or len(name) > 100:
            return bad("client_name must be a string of at most 100 characters")
        method = body.get("token_endpoint_auth_method") or "none"
        if method not in AUTH_METHODS:
            return bad("Unsupported token_endpoint_auth_method")
        grants = body.get("grant_types")
        if grants is not None and (not isinstance(grants, list)
                                   or "authorization_code" not in grants):
            return bad("grant_types must include authorization_code")

        clients = request.env["aidoo.oauth.client"].sudo()
        if clients._unused_count() >= MAX_UNUSED_CLIENTS:
            return _json({"error": "temporarily_unavailable",
                          "error_description": "Too many unused clients"},
                         status=429)
        client, secret = clients._register_client(name.strip() or "MCP client",
                                           uris, method)
        data = {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.create_date.timestamp()),
            "client_name": client.name,
            "redirect_uris": uris,
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": SCOPE,
        }
        if secret:
            data["client_secret"] = secret
            data["client_secret_expires_at"] = 0
        return _json(data, status=201, headers=NO_STORE)

    # --- authorization endpoint -----------------------------------------
    def _error_page(self, message, status=400):
        resp = request.render("aidoo.oauth_error", {"message": message})
        resp.status_code = status
        return resp

    def _check_authorize(self, kw):
        """Return (client, redirect_uri, error). A non-None error is a
        message to show on a page (never redirected)."""
        client = request.env["aidoo.oauth.client"].sudo().search(
            [("client_id", "=", kw.get("client_id") or "")], limit=1)
        uri = kw.get("redirect_uri") or ""
        if not client or uri not in client._uris():
            return None, None, "Unknown client or redirect URI."
        return client, uri, None

    def _authorize_problem(self, kw):
        """OAuth parameter problem to report to the client by redirect."""
        if kw.get("response_type") != "code":
            return "unsupported_response_type"
        if kw.get("code_challenge_method") != "S256" \
                or not kw.get("code_challenge"):
            return "invalid_request"
        if not 43 <= len(kw["code_challenge"]) <= 128:
            return "invalid_request"
        if kw.get("scope") and SCOPE not in kw["scope"].split():
            return "invalid_scope"
        return None

    @http.route("/aidoo/oauth/authorize", type="http", auth="public",
                methods=["GET", "POST"], readonly=False)
    def authorize(self, **kw):
        if (off := _disabled()):
            return off
        user = request.env.user
        if user._is_public():
            # auth="public" so the disabled switch is checked first; send
            # anonymous browsers through the normal Odoo login.
            target = request.httprequest.full_path.rstrip("?")
            return request.redirect(
                "/web/login?" + urlencode({"redirect": target}), code=303)
        client, uri, err = self._check_authorize(kw)
        if err:
            return self._error_page(err)
        if not (user.has_group("aidoo.group_ai_user")
                or user.has_group("aidoo.group_ai_manager")):
            return self._error_page(
                "Your Odoo user is not allowed to use Aidoo. Ask an "
                "administrator for the AI Assistant User group.", 403)
        state = kw.get("state")
        if (problem := self._authorize_problem(kw)):
            return _redirect_with(uri, {"error": problem, "state": state})

        def consent(error=None):
            return request.render("aidoo.oauth_consent", {
                "client_name": client.name,
                "redirect_host": urlsplit(uri).netloc,
                "user_name": user.name,
                "error": error,
                "hidden": {k: kw.get(k) for k in (
                    "client_id", "redirect_uri", "response_type", "state",
                    "code_challenge", "code_challenge_method", "scope")
                    if kw.get(k) is not None},
            })

        if request.httprequest.method == "POST":
            decision = kw.get("decision")
            _logger.info(
                "Aidoo OAuth consent: client=%s user=%s decision=%s",
                client.client_id, user.login,
                decision if decision in ("allow", "deny") else "invalid")
            if decision == "deny":
                return _redirect_with(
                    uri, {"error": "access_denied", "state": state})
            if decision != "allow":
                return consent("No decision was received. Please choose "
                               "Allow or Deny.")
            code = request.env["aidoo.oauth.grant"].sudo()._issue_code(
                client, user, uri, kw["code_challenge"])
            return _redirect_with(uri, {"code": code, "state": state})

        return consent()

    # --- token endpoint -------------------------------------------------
    @http.route("/aidoo/oauth/token", type="http", auth="none",
                methods=["POST"], cors="*", csrf=False, readonly=False)
    def token(self, **kw):
        if (off := _disabled()):
            return off

        def err(code, desc=None, status=400, headers=None):
            data = {"error": code}
            if desc:
                data["error_description"] = desc
            return _json(data, status=status, headers=NO_STORE + (headers or []))

        client_id, secret = kw.get("client_id"), kw.get("client_secret")
        basic = request.httprequest.authorization
        if basic and basic.type == "basic":
            client_id = unquote_plus(basic.username or "")
            secret = unquote_plus(basic.password or "")
        client = request.env["aidoo.oauth.client"].sudo().search(
            [("client_id", "=", client_id or "")], limit=1)
        if not client or not client._check_secret(secret):
            return err("invalid_client", status=401,
                       headers=[("WWW-Authenticate", "Basic")])

        grants = request.env["aidoo.oauth.grant"].sudo()
        grant_type = kw.get("grant_type")
        if grant_type == "authorization_code":
            result = grants._exchange_code(
                kw.get("code") or "", kw.get("code_verifier"), client,
                kw.get("redirect_uri") or "")
        elif grant_type == "refresh_token":
            result = grants._refresh(kw.get("refresh_token") or "", client)
        else:
            return err("unsupported_grant_type")
        if not result:
            return err("invalid_grant")
        return _json(result, headers=NO_STORE)
