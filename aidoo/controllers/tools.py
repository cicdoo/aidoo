# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Loopback HTTP tool endpoints for the stdio MCP bridge.

These routes are the bridge's only door into Odoo (CLI-driven providers reach
them over loopback HTTP, authenticated with the per-session bearer token). They
are deliberately THIN: every route forwards to the shared ``aidoo.tool.exec``
model, which is the single place that enforces the per-user tool grant, the
model denylist, su=False, and the immutable audit log — identical behaviour to
the native in-process providers that call the same executor directly.
"""
from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

# The SQL read-only validator + tool enforcement now live in the shared
# ``aidoo.tool.exec`` model. Re-export the validators here for back-compat with
# existing imports (e.g. tests/test_sql_guard.py) that pinned this location.
from ..models.aidoo_tool_exec import (  # noqa: F401
    _validate_select,
    _strip_sql_literals,
)


class AiAssistantTools(http.Controller):

    def _dispatch(self, tool, arguments):
        """Forward one bridge tool call to the shared executor as the user."""
        if request.env.su:
            # Defense in depth: the executor guards this too, but never even
            # reach it as superuser.
            raise AccessError("Tool endpoint running as superuser — aborting.")
        session = request.ai_session
        ip = request.httprequest.remote_addr
        return request.env["aidoo.tool.exec"]._dispatch(
            session, tool, arguments or {}, client_ip=ip)

    # --- read tools ---------------------------------------------------
    @http.route("/aidoo/tool/model_introspect", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def model_introspect(self, **kw):
        return self._dispatch("model_introspect", kw)

    @http.route("/aidoo/tool/orm_search_read", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def orm_search_read(self, **kw):
        return self._dispatch("orm_search_read", kw)

    @http.route("/aidoo/tool/orm_read", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def orm_read(self, **kw):
        return self._dispatch("orm_read", kw)

    @http.route("/aidoo/tool/orm_call", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def orm_call(self, **kw):
        return self._dispatch("orm_call", kw)

    @http.route("/aidoo/tool/sql_select", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def sql_select(self, **kw):
        return self._dispatch("sql_select", kw)

    # --- write tools --------------------------------------------------
    @http.route("/aidoo/tool/orm_create", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def orm_create(self, **kw):
        return self._dispatch("orm_create", kw)

    @http.route("/aidoo/tool/orm_write", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def orm_write(self, **kw):
        return self._dispatch("orm_write", kw)

    @http.route("/aidoo/tool/orm_unlink", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def orm_unlink(self, **kw):
        return self._dispatch("orm_unlink", kw)

    # --- action tools -------------------------------------------------
    @http.route("/aidoo/tool/orm_action", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def orm_action(self, **kw):
        return self._dispatch("orm_action", kw)

    @http.route("/aidoo/tool/run_wizard", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def run_wizard(self, **kw):
        return self._dispatch("run_wizard", kw)

    @http.route("/aidoo/tool/run_server_action", type="jsonrpc",
                auth="ai_bridge", methods=["POST"])
    def run_server_action(self, **kw):
        return self._dispatch("run_server_action", kw)
