# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Minimal OAuth 2.1 authorization server state for the remote MCP endpoint.

Access tokens are ordinary Odoo API keys (scope ``aidoo_mcp``) so the bearer
check, group check, tool executor and audit log stay unchanged. Codes, refresh
tokens and client secrets are only stored as SHA-256 hashes.
"""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from odoo import api, fields, models

SCOPE = "aidoo_mcp"
CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
UNUSED_CLIENT_TTL = timedelta(days=30)
MAX_UNUSED_CLIENTS = 200


def _hash(value):
    return hashlib.sha256((value or "").encode()).hexdigest()


def _pkce_ok(verifier, challenge):
    if not verifier or not challenge or not (43 <= len(verifier) <= 128):
        return False
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


class AidooOauthClient(models.Model):
    _name = "aidoo.oauth.client"
    _description = "Aidoo OAuth Client"

    client_id = fields.Char(required=True, index=True, readonly=True)
    secret_hash = fields.Char(readonly=True)
    name = fields.Char(required=True)
    redirect_uris = fields.Text(required=True, help="JSON list of redirect URIs")
    auth_method = fields.Selection(
        [("none", "none"), ("client_secret_post", "client_secret_post"),
         ("client_secret_basic", "client_secret_basic")],
        default="none", required=True)
    grant_ids = fields.One2many("aidoo.oauth.grant", "client_id")

    _sql_constraints = [
        ("client_id_uniq", "UNIQUE(client_id)", "The client id must be unique."),
    ]

    def _uris(self):
        self.ensure_one()
        try:
            uris = json.loads(self.redirect_uris or "[]")
        except ValueError:
            return []
        return uris if isinstance(uris, list) else []

    @api.model
    def _register_client(self, name, redirect_uris, auth_method):
        """Create a client; return (record, plain_secret or None)."""
        secret = secrets.token_urlsafe(32) if auth_method != "none" else None
        client = self.create({
            "client_id": secrets.token_urlsafe(24),
            "secret_hash": _hash(secret) if secret else False,
            "name": name,
            "redirect_uris": json.dumps(redirect_uris),
            "auth_method": auth_method,
        })
        return client, secret

    @api.model
    def _unused_count(self):
        return self.search_count([("grant_ids", "=", False)])

    def _check_secret(self, secret):
        self.ensure_one()
        if self.auth_method == "none":
            return True
        return bool(secret and self.secret_hash
                    and hmac.compare_digest(self.secret_hash, _hash(secret)))

    @api.autovacuum
    def _gc_unused_clients(self):
        limit = fields.Datetime.now() - UNUSED_CLIENT_TTL
        self.search([("grant_ids", "=", False),
                     ("create_date", "<", limit)]).unlink()


class AidooOauthGrant(models.Model):
    _name = "aidoo.oauth.grant"
    _description = "Aidoo OAuth Grant"

    client_id = fields.Many2one("aidoo.oauth.client", required=True,
                                ondelete="cascade", index=True)
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade",
                              index=True)
    code_hash = fields.Char(index=True)
    code_expires = fields.Datetime()
    code_challenge = fields.Char()
    redirect_uri = fields.Char()
    used = fields.Boolean(help="The authorization code was already redeemed.")
    refresh_hash = fields.Char(index=True)
    prev_refresh_hash = fields.Char(index=True)
    refresh_expires = fields.Datetime()
    apikey_id = fields.Integer(help="res.users.apikeys id of the access token")

    @api.model
    def _issue_code(self, client, user, redirect_uri, code_challenge):
        code = secrets.token_urlsafe(32)
        self.create({
            "client_id": client.id,
            "user_id": user.id,
            "code_hash": _hash(code),
            "code_expires": fields.Datetime.now() + CODE_TTL,
            "code_challenge": code_challenge,
            "redirect_uri": redirect_uri,
        })
        return code

    @api.model
    def _exchange_code(self, code, verifier, client, redirect_uri):
        """Return the token dict, or None when the code is not acceptable."""
        grant = self.search([("code_hash", "=", _hash(code)),
                             ("client_id", "=", client.id)], limit=1)
        if not grant:
            return None
        if grant.used:
            grant.unlink()  # replay: revoke everything issued from this code
            return None
        grant.used = True
        if (not grant.code_expires or grant.code_expires < fields.Datetime.now()
                or grant.redirect_uri != redirect_uri
                or not _pkce_ok(verifier, grant.code_challenge)):
            return None
        return grant._mint_tokens()

    def _remove_key(self):
        keys = self.env["res.users.apikeys"].sudo().browse(
            [g.apikey_id for g in self if g.apikey_id]).exists()
        if keys:
            keys._remove()

    def _mint_tokens(self):
        self.ensure_one()
        user = self.user_id
        self._remove_key()
        # Honour the Odoo group limit on API key duration.
        days = max((g.api_key_duration for g in user.sudo().groups_id),
                   default=0) or 1.0
        ttl = min(ACCESS_TTL, timedelta(days=days))
        apikeys = self.env["res.users.apikeys"].with_user(user).sudo()
        key = apikeys._generate(
            SCOPE, "Aidoo OAuth: %s" % self.client_id.name[:80],
            datetime.now() + ttl)
        self.env.cr.execute(
            "SELECT id FROM res_users_apikeys WHERE user_id = %s AND index = %s"
            " ORDER BY id DESC LIMIT 1", (user.id, key[:8]))
        key_id = self.env.cr.fetchone()[0]
        refresh = secrets.token_urlsafe(48)
        self.write({
            "apikey_id": key_id,
            "refresh_hash": _hash(refresh),
            "refresh_expires": fields.Datetime.now() + REFRESH_TTL,
        })
        return {
            "access_token": key,
            "token_type": "Bearer",
            "expires_in": int(ttl.total_seconds()),
            "refresh_token": refresh,
            "scope": SCOPE,
        }

    @api.model
    def _refresh(self, token, client):
        """Rotate a refresh token; return the token dict or None."""
        h = _hash(token)
        reused = self.search([("prev_refresh_hash", "=", h),
                              ("client_id", "=", client.id)])
        if reused:
            reused.unlink()  # a rotated token came back: revoke the grant
            return None
        grant = self.search([("refresh_hash", "=", h),
                             ("client_id", "=", client.id)], limit=1)
        if not grant or not grant.refresh_expires \
                or grant.refresh_expires < fields.Datetime.now():
            return None
        grant.prev_refresh_hash = h
        return grant._mint_tokens()

    def unlink(self):
        self._remove_key()
        return super().unlink()

    @api.autovacuum
    def _gc_expired(self):
        now = fields.Datetime.now()
        self.search(["|",
                     "&", ("refresh_hash", "=", False), ("code_expires", "<", now),
                     "&", ("refresh_hash", "!=", False),
                     ("refresh_expires", "<", now)]).unlink()
