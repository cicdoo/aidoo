# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Remote MCP endpoint (/aidoo/mcp): auth, gating, protocol and audit."""
import importlib.util
import json
import os
from datetime import datetime, timedelta

from odoo.addons.aidoo.models.mcp_tools import MCP_TOOL_DEFS
from odoo.tests.common import HttpCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestAidooMcp(HttpCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("aidoo.mcp_enabled", "1")
        self.user = self.env["res.users"].create({
            "name": "Aidoo MCP User", "login": "aidoo_mcp_user",
            "password": "aidoo_mcp_pw_123!",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("aidoo.group_ai_user").id])],
        })
        self.key = self._key(self.user, "aidoo_mcp")

    def _key(self, user, scope):
        return self.env["res.users.apikeys"].with_user(user)._generate(
            scope, "test", datetime.now() + timedelta(days=1))

    def _post(self, body, key=None, headers=None, raw=False):
        h = {"Content-Type": "application/json"}
        if key is not False:
            h["Authorization"] = "Bearer %s" % (key or self.key)
        h.update(headers or {})
        data = body if raw else json.dumps(body)
        return self.url_open("/aidoo/mcp", data=data, headers=h)

    def _rpc(self, method, params=None, **kw):
        return self._post({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}}, **kw)

    def _call(self, name, arguments, **kw):
        return self._rpc("tools/call", {"name": name, "arguments": arguments},
                         **kw).json()["result"]

    def test_unset_parameter_means_enabled(self):
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", "aidoo.mcp_enabled")]).unlink()
        self.assertEqual(self._rpc("initialize").status_code, 200)

    def test_disabled_returns_403_with_hint(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.mcp_enabled", "False")
        res = self._rpc("ping")
        self.assertEqual(res.status_code, 403)
        self.assertIn("Settings", res.json()["hint"])

    def test_get_describes_endpoint(self):
        res = self.url_open("/aidoo/mcp",
                            headers={"Authorization": "Bearer %s" % self.key})
        self.assertEqual(res.status_code, 405)
        self.assertEqual(res.headers.get("Allow"), "POST")
        self.assertEqual(res.json()["endpoint"], "/aidoo/mcp")

    def test_auth(self):
        self.assertEqual(self._rpc("ping", key=False).status_code, 401)
        self.assertEqual(self._rpc("ping", key="bogus").status_code, 401)
        rpc_key = self._key(self.user, "rpc")
        self.assertEqual(self._rpc("ping", key=rpc_key).status_code, 401)

    def test_user_without_group_forbidden(self):
        other = self.env["res.users"].create({
            "name": "No AI", "login": "aidoo_mcp_noai", "password": "x_pw_123456!",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])]})
        self.assertEqual(
            self._rpc("ping", key=self._key(other, "aidoo_mcp")).status_code, 403)

    def test_initialize_and_ping(self):
        res = self._rpc("initialize", {"protocolVersion": "2025-06-18"}).json()
        self.assertEqual(res["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(res["result"]["serverInfo"]["name"], "aidoo")
        self.assertIn("tools", res["result"]["capabilities"])
        res = self._rpc("initialize", {"protocolVersion": "1999-01-01"}).json()
        self.assertEqual(res["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(self._rpc("ping").json()["result"], {})

    def test_unknown_method_and_get(self):
        self.assertEqual(self._rpc("nope").json()["error"]["code"], -32601)
        resp = self.url_open(
            "/aidoo/mcp", headers={"Authorization": "Bearer %s" % self.key})
        self.assertEqual(resp.status_code, 405)

    def test_tools_list_follows_grant(self):
        names = {t["name"] for t in
                 self._rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual(names, self.user._ai_effective_tools())
        self.assertNotIn("orm_create", names)
        tool = self.env["aidoo.tool"].search([("name", "=", "orm_create")])
        read = self.env["aidoo.tool"].search([("name", "=", "orm_search_read")])
        self.user.aidoo_tool_ids = tool | read
        self.user.aidoo_zero_trust_mode = "off"
        names = {t["name"] for t in
                 self._rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual(names, {"orm_create", "orm_search_read"})
        self.user.aidoo_zero_trust_mode = "on"
        names = {t["name"] for t in
                 self._rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual(names, {"orm_search_read"})

    def test_search_read_and_audit(self):
        partner = self.env["res.partner"].create({"name": "MCP Probe Partner"})
        result = self._call("orm_search_read", {
            "model": "res.partner", "domain": [["id", "=", partner.id]],
            "fields": ["name"]})
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["records"][0]["name"], "MCP Probe Partner")
        log = self.env["aidoo.tool_log"].sudo().search(
            [("user_id", "=", self.user.id), ("tool", "=", "orm_search_read")],
            limit=1, order="id desc")
        self.assertTrue(log)
        self.assertFalse(log.session_id)
        self.assertEqual(log.outcome, "success")

    def test_record_rules_respected(self):
        # Portal-level data the plain internal user cannot read: ir.config_parameter
        # has no ACL for internal users.
        result = self._call("orm_search_read", {"model": "ir.config_parameter"})
        self.assertTrue(result["isError"])

    def test_excluded_model_is_error(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.excluded_models", "res.partner")
        result = self._call("orm_search_read", {"model": "res.partner"})
        self.assertTrue(result["isError"])

    def test_ungranted_tool_denied_and_logged(self):
        result = self._call("orm_create", {
            "model": "res.partner", "values": {"name": "nope"}})
        self.assertTrue(result["isError"])
        self.assertFalse(self.env["res.partner"].search([("name", "=", "nope")]))
        log = self.env["aidoo.tool_log"].sudo().search(
            [("user_id", "=", self.user.id), ("tool", "=", "orm_create")],
            limit=1, order="id desc")
        self.assertEqual(log.outcome, "access_denied")

    def test_unknown_tool_is_invalid_params(self):
        res = self._rpc("tools/call", {"name": "does_not_exist"}).json()
        self.assertEqual(res["error"]["code"], -32602)

    def test_foreign_origin_rejected(self):
        resp = self._rpc("ping", headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)
        resp = self._rpc("ping", headers={"Origin": self.base_url()})
        self.assertEqual(resp.status_code, 200)

    def test_batch_notification_and_parse_error(self):
        resp = self._post([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual([r["id"] for r in resp.json()], [1, 2])
        resp = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(resp.status_code, 202)
        self.assertFalse(resp.content)
        resp = self._post("{not json", raw=True)
        self.assertEqual(resp.json()["error"]["code"], -32700)
        resp = self._post({"foo": "bar"})
        self.assertEqual(resp.json()["error"]["code"], -32600)

    def test_bridge_tools_match_shared_defs(self):
        path = os.path.join(os.path.dirname(__file__), "..", "bridge",
                            "mcp_server.py")
        spec = importlib.util.spec_from_file_location("aidoo_bridge_copy", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.TOOLS, MCP_TOOL_DEFS)
