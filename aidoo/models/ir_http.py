# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import logging
import re

from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import Unauthorized

from odoo import models
from odoo.exceptions import AccessDenied
from odoo.http import request

_logger = logging.getLogger(__name__)


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _auth_method_ai_bridge(cls):
        """Custom auth for the AI tool endpoints.

        Validates the per-session bridge token from the Authorization header,
        rebinds the whole request environment to the session's user (su=False),
        and pins the request to loopback. After this runs, request.env enforces
        that user's ACLs and record rules automatically.
        """
        # Only accept loopback callers (the MCP bridge runs on the same host).
        remote = request.httprequest.remote_addr
        if remote not in ("127.0.0.1", "::1", "localhost"):
            _logger.warning("ai_bridge: rejected non-loopback caller %s", remote)
            raise AccessDenied()

        auth = request.httprequest.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise AccessDenied()
        token = auth[len("Bearer "):].strip()

        session = request.env["aidoo.session"].sudo()._verify_bridge_token(token)
        if not session:
            _logger.warning("ai_bridge: invalid/expired token from %s", remote)
            raise AccessDenied()

        uid = session.user_id.id
        # Rebind the request env to the target user (su=False → ACLs enforced).
        request.update_env(user=uid)
        # Make the validated session available to the tool handlers.
        request.ai_session = session.with_user(uid)

    @classmethod
    def _auth_method_aidoo_mcp(cls):
        """Bearer auth for ``/aidoo/mcp`` that advertises OAuth discovery.

        Accepts an API key of scope ``aidoo_mcp`` (Odoo's check also accepts
        unscoped keys). A 401 carries the RFC 9728 ``resource_metadata``
        pointer MCP clients use to find the authorization server.
        """
        header = request.httprequest.headers.get("Authorization")
        match = re.match(r"^bearer\s+(.+)$", header or "", re.IGNORECASE)
        uid = None
        if match:
            uid = request.env["res.users.apikeys"]._check_credentials(
                scope="aidoo_mcp", key=match.group(1))
        if not uid:
            values = {"resource_metadata": cls._aidoo_resource_metadata_url()}
            if header:
                values["error"] = "invalid_token"
            raise Unauthorized(
                "Invalid or missing API key",
                www_authenticate=WWWAuthenticate("bearer", values))
        request.update_env(user=uid)

    @classmethod
    def _aidoo_resource_metadata_url(cls):
        base = request.httprequest.host_url.rstrip("/")
        return base + "/.well-known/oauth-protected-resource/aidoo/mcp"
