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

    # ------------------------------------------------------------------
    # Skills: /aidoo/new with a skill, /aidoo/skill toggle, unreadable reject
    # ------------------------------------------------------------------
    def test_new_and_set_skill(self):
        skill = self.env["aidoo.skill"].with_user(self.user).create(
            {"name": "Tabler", "instructions": "Answer in a table."})
        self.authenticate("aidoo_routes_user", self.password)

        created = self._call("/aidoo/new",
                             {"provider": "claude", "skill_id": skill.id})
        self.assertEqual(created["result"]["skill_id"], skill.id)
        sess = self.env["aidoo.session"].browse(created["result"]["id"])
        self.assertEqual(sess.skill_id, skill)

        # Clear it via /aidoo/skill, then set it back.
        self._call("/aidoo/skill", {"session_id": self.conv.id, "skill_id": False})
        self.conv.invalidate_recordset(["skill_id"])
        self.assertFalse(self.conv.skill_id)
        self._call("/aidoo/skill",
                   {"session_id": self.conv.id, "skill_id": skill.id})
        self.conv.invalidate_recordset(["skill_id"])
        self.assertEqual(self.conv.skill_id, skill)

    def test_set_skill_rejects_unreadable_skill(self):
        """A private skill owned by another user can't be attached."""
        other = self.env["res.users"].create({
            "name": "SkillOwner", "login": "aidoo_skillowner",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("aidoo.group_ai_user").id])],
        })
        private = self.env["aidoo.skill"].with_user(other).create(
            {"name": "Secret", "instructions": "x"})
        self.authenticate("aidoo_routes_user", self.password)
        self._call("/aidoo/skill",
                   {"session_id": self.conv.id, "skill_id": private.id})
        # Not readable -> resolved to empty -> session keeps no skill.
        self.conv.invalidate_recordset(["skill_id"])
        self.assertFalse(self.conv.skill_id)

    # ------------------------------------------------------------------
    # Deleting a whole conversation (/aidoo/delete)
    # ------------------------------------------------------------------
    def test_delete_conversation_removes_it_with_messages(self):
        self.env["aidoo.message"].create(
            {"session_id": self.conv.id, "role": "user", "body": "hi"})
        sid = self.conv.id
        self.authenticate("aidoo_routes_user", self.password)
        body = self._call("/aidoo/delete", {"session_id": sid})
        self.assertTrue(body["result"]["deleted"])
        self.assertFalse(self.env["aidoo.session"].sudo().browse(sid).exists())

    def test_delete_refuses_while_running(self):
        self.conv.state = "running"
        self.authenticate("aidoo_routes_user", self.password)
        body = self._call("/aidoo/delete", {"session_id": self.conv.id})
        self.assertFalse(body["result"]["deleted"])
        self.assertEqual(body["result"].get("reason"), "running")
        self.assertTrue(self.conv.exists())

    def test_delete_rejects_other_users_session(self):
        other = self.env["res.users"].create({
            "name": "Owner2", "login": "aidoo_owner2",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("aidoo.group_ai_user").id])],
        })
        foreign = self.env["aidoo.session"].create(
            {"user_id": other.id, "provider": "claude"})
        self.authenticate("aidoo_routes_user", self.password)
        body = self._call("/aidoo/delete", {"session_id": foreign.id})
        self.assertIn("error", body)
        self.assertTrue(foreign.exists())
