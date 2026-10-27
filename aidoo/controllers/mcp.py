# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Remote MCP server (Streamable HTTP, JSON-only) at ``/aidoo/mcp``.

External MCP clients authenticate with a user's own Odoo API key of scope
``aidoo_mcp`` (Odoo's built-in ``bearer`` auth). Every ``tools/call`` goes through
the shared ``aidoo.tool.exec`` executor, so the per-user tool grant, model
denylist, non-superuser execution and the ``aidoo.tool_log`` audit are identical
to the in-chat path. Disabled unless ``aidoo.mcp_enabled`` is set.
"""
import json
import logging
from urllib.parse import urlsplit

from odoo import http
from odoo.http import request
from odoo.tools import str2bool

from ..models.mcp_tools import MCP_TOOL_DEFS, visible_tools

_logger = logging.getLogger(__name__)

MAX_BODY = 1024 * 1024
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_VERSION = "2025-03-26"


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


class AidooMcp(http.Controller):

    @http.route("/aidoo/mcp", type="http", auth="bearer",
                bearer_scope="aidoo_mcp", methods=["POST", "GET", "DELETE"],
                csrf=False, readonly=False)
    def mcp(self, **kw):
        env = request.env
        icp = env["ir.config_parameter"].sudo()
        if not str2bool(icp.get_str("aidoo.mcp_enabled", ""), False):
            return request.not_found()
        if env.su:
            return request.make_json_response({"error": "forbidden"}, status=403)
        user = env.user
        if not (user.has_group("aidoo.group_ai_user")
                or user.has_group("aidoo.group_ai_manager")):
            return request.make_json_response({"error": "forbidden"}, status=403)
        if not self._origin_ok():
            return request.make_json_response(
                {"error": "origin not allowed"}, status=403)

        httpreq = request.httprequest
        if httpreq.method != "POST":
            return request.make_response(
                "", status=405, headers=[("Allow", "POST")])
        if (httpreq.content_length or 0) > MAX_BODY:
            return request.make_json_response(
                _err(None, -32600, "Request too large"), status=413)
        raw = httpreq.get_data(cache=False)
        if len(raw) > MAX_BODY:
            return request.make_json_response(
                _err(None, -32600, "Request too large"), status=413)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return request.make_json_response(_err(None, -32700, "Parse error"))

        if isinstance(payload, list):
            if not payload:
                return request.make_json_response(
                    _err(None, -32600, "Invalid Request"))
            out = [r for r in (self._handle(m) for m in payload) if r is not None]
            if not out:
                return request.make_response("", status=202)
            return request.make_json_response(out)
        resp = self._handle(payload)
        if resp is None:
            return request.make_response("", status=202)
        return request.make_json_response(resp)

    @staticmethod
    def _origin_ok():
        """DNS-rebinding guard: a browser-supplied Origin must be our own host."""
        origin = request.httprequest.headers.get("Origin")
        if not origin:
            return True
        host = urlsplit(origin).netloc
        return bool(host) and host == request.httprequest.host

    def _handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" \
                or not isinstance(msg.get("method"), str):
            return _err(msg.get("id") if isinstance(msg, dict) else None,
                        -32600, "Invalid Request")
        method = msg["method"]
        req_id = msg.get("id")
        is_notification = "id" not in msg
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _err(req_id, -32600, "Invalid Request")
        if is_notification:
            return None  # notifications/* (and any other) get no response

        env = request.env
        if method == "initialize":
            version = params.get("protocolVersion")
            if version not in SUPPORTED_VERSIONS:
                version = DEFAULT_VERSION
            return _ok(req_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "aidoo", "version": "1.0.0"},
            })
        if method == "ping":
            return _ok(req_id, {})
        if method == "tools/list":
            allowed = env["aidoo.tool.exec"]._effective_tools(None)
            return _ok(req_id, {"tools": visible_tools(allowed)})
        if method == "tools/call":
            name = params.get("name")
            # Ungranted-but-real tools still reach the executor so the denial
            # is audited; only names we do not define are protocol errors.
            if name not in {t["name"] for t in MCP_TOOL_DEFS}:
                return _err(req_id, -32602, "Unknown tool: %s" % name)
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _err(req_id, -32602, "arguments must be an object")
            result = env["aidoo.tool.exec"]._dispatch(
                None, name, arguments,
                client_ip=request.httprequest.remote_addr)
            is_error = isinstance(result, dict) and "error" in result
            return _ok(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    result, default=str, ensure_ascii=False)}],
                "isError": bool(is_error),
            })
        return _err(req_id, -32601, "Method not found: %s" % method)
