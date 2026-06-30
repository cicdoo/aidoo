# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""End-to-end checks of the user-facing JSON routes that the OWL client calls.

Covers the bookmark toggle (``/aidoo/favorite``): it persists the flag on the
conversation and only ever touches the caller's own sessions (the record rules
on aidoo.session enforce ownership)."""
import json

from odoo.tests.common import HttpCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestAidooRoutes(HttpCase):

    def setUp(self):
        super().setUp()
        self.password = "aidoo_routes_pw"
        self.user = self.env["res.users"].create({
            "name": "Aidoo Routes User",
            "login": "aidoo_routes_user",
            "password": self.password,
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("aidoo.group_ai_user").id,
            ])],
        })
        self.conv = self.env["aidoo.session"].create(
            {"user_id": self.user.id, "provider": "claude"})

    def _call(self, route, params):
        payload = {"jsonrpc": "2.0", "method": "call", "params": params}
        resp = self.url_open(
            route, data=json.dumps(payload),
            headers={"Content-Type": "application/json"})
        return resp.json()

    def test_favorite_toggles_and_persists(self):
        self.authenticate("aidoo_routes_user", self.password)
        self.assertFalse(self.conv.is_favorite)

        body = self._call("/aidoo/favorite",
                          {"session_id": self.conv.id, "is_favorite": True})
        self.assertEqual(body["result"]["is_favorite"], True)
        self.conv.invalidate_recordset(["is_favorite"])
        self.assertTrue(self.conv.is_favorite)

        # Toggling back off persists too.
        body = self._call("/aidoo/favorite",
                          {"session_id": self.conv.id, "is_favorite": False})
        self.assertEqual(body["result"]["is_favorite"], False)
        self.conv.invalidate_recordset(["is_favorite"])
        self.assertFalse(self.conv.is_favorite)

    def test_favorite_rejects_other_users_session(self):
        """A user cannot star someone else's conversation (record-rule scoped)."""
        other = self.env["res.users"].create({
            "name": "Other", "login": "aidoo_routes_other",
            "password": "x",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("aidoo.group_ai_user").id])],
        })
        other_session = self.env["aidoo.session"].create(
            {"user_id": other.id, "provider": "claude"})
        self.authenticate("aidoo_routes_user", self.password)
        body = self._call("/aidoo/favorite",
                          {"session_id": other_session.id, "is_favorite": True})
        # The route resolves sessions through the caller's own rights, so the
        # foreign session is not found / not writable — an error, not a silent
        # cross-user write.
        self.assertIn("error", body)
        other_session.invalidate_recordset(["is_favorite"])
        self.assertFalse(other_session.is_favorite)
