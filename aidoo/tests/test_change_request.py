# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Change requests and the tool-call guard.

The contract: a persona can propose a change through ``orm_action`` but can
never approve one, not even when the session belongs to its principal; only a
principal, acting in Odoo, applies it, and only if the target has not moved.
"""
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.aidoo.models.aidoo_guard import require_human


@tagged("post_install", "-at_install", "aidoo")
class TestChangeRequest(TransactionCase):

    def setUp(self):
        super().setUp()
        p = patch.object(type(self.env["aidoo.tool_log"]), "_record",
                         lambda self, vals: None)
        p.start()
        self.addCleanup(p.stop)
        ai = [(4, self.env.ref("base.group_user").id),
              (4, self.env.ref("aidoo.group_ai_user").id)]
        self.agent = self.env["res.users"].create({
            "name": "Run-as Agent", "login": "aidoo_cr_agent", "group_ids": ai})
        self.boss = self.env["res.users"].create({
            "name": "Principal", "login": "aidoo_cr_boss",
            "group_ids": ai + [(4, self.env.ref("base.group_partner_manager").id)]})
        self.other = self.env["res.users"].create({
            "name": "Teammate", "login": "aidoo_cr_other",
            "group_ids": ai + [(4, self.env.ref("base.group_partner_manager").id)]})
        tools = self.env["aidoo.tool"].search([("name", "in", [
            "orm_read", "orm_search_read", "orm_action", "orm_create", "orm_write"])])
        for user in (self.agent, self.boss):
            user.aidoo_tool_ids = [(6, 0, tools.ids)]
        self.persona = self.env["aidoo.persona"].create({
            "name": "PlannerBot", "provider": "claude",
            "principal_user_ids": [(6, 0, self.boss.ids)]})
        self.partner = self.env["res.partner"].create({"name": "Acme"})

    def _session(self, user):
        return self.env["aidoo.session"].with_user(user).create({
            "user_id": user.id, "provider": "claude",
            "persona_id": self.persona.id})

    def _tool(self, user, tool, args, session=None):
        return self.env["aidoo.tool.exec"].with_user(user)._dispatch(
            session or self._session(user), tool, args)

    def _propose(self, **kw):
        kwargs = {"model": "res.partner", "operation": "write",
                  "res_ids": [self.partner.id], "values": {"name": "Acme Ltd"},
                  "summary": "Rename Acme"}
        kwargs.update(kw)
        res = self._tool(self.agent, "orm_action", {
            "model": "aidoo.change.request", "method": "action_propose",
            "kwargs": kwargs})
        self.assertNotIn("error", res, res)
        return self.env["aidoo.change.request"].browse(res["action"]["id"])

    def test_propose_records_a_draft_and_changes_nothing(self):
        req = self._propose()
        self.assertEqual(req.state, "draft")
        self.assertEqual(req.persona_id, self.persona)
        self.assertEqual(req.proposer_id, self.agent)
        self.assertEqual(self.partner.name, "Acme")

    def test_agent_cannot_approve_even_in_the_principals_session(self):
        req = self._propose()
        res = self._tool(self.boss, "orm_action", {
            "model": "aidoo.change.request", "ids": req.ids,
            "method": "action_approve"})
        self.assertIn("error", res)
        self.assertEqual(req.state, "draft")
        self.assertEqual(self.partner.name, "Acme")

    def test_rows_cannot_be_forged_with_orm_create_or_write(self):
        res = self._tool(self.agent, "orm_create", {
            "model": "aidoo.change.request",
            "values": {"summary": "x", "model": "res.partner",
                       "operation": "unlink", "state": "applied"}})
        self.assertIn("error", res)
        req = self._propose()
        res = self._tool(self.boss, "orm_write", {
            "model": "aidoo.change.request", "ids": req.ids,
            "values": {"state": "applied"}})
        self.assertIn("error", res)
        self.assertEqual(req.state, "draft")

    def test_only_a_principal_decides(self):
        req = self._propose()
        with self.assertRaises(AccessError):
            req.with_user(self.other).action_approve()
        req.with_user(self.boss).action_approve()
        self.assertEqual(req.state, "applied")
        self.assertEqual(req.approved_by_id, self.boss)
        self.assertEqual(self.partner.name, "Acme Ltd")

    def test_no_principal_means_nobody_can_approve(self):
        req = self._propose()
        self.persona.principal_user_ids = False
        with self.assertRaises(AccessError):
            req.with_user(self.boss).action_approve()

    def test_moved_target_goes_stale(self):
        req = self._propose()
        self.partner.name = "Acme Holdings"
        req.with_user(self.boss).action_approve()
        self.assertEqual(req.state, "stale")
        self.assertEqual(self.partner.name, "Acme Holdings")

    def test_denied_method_is_refused_directly_but_can_be_proposed(self):
        res = self._tool(self.boss, "orm_action", {
            "model": "res.partner", "ids": self.partner.ids,
            "method": "action_archive"})
        self.assertIn("error", res)
        self.assertTrue(self.partner.active)
        req = self._propose(operation="action", method="action_archive",
                            values={}, summary="Archive Acme")
        req.with_user(self.boss).action_approve()
        self.assertEqual(req.state, "applied")
        self.assertFalse(self.partner.active)

    def test_require_human_only_fires_under_a_tool_call(self):
        require_human(self.env)
        with self.assertRaises(AccessError):
            require_human(self.env(context=dict(self.env.context, aidoo_tool_call=True)))

    def test_principal_line_is_set_by_the_server(self):
        persona = self.persona.sudo()
        line = self._session(self.boss)._principal_line(persona)
        self.assertIn("IS one of your principals", line)
        line = self._session(self.other)._principal_line(persona)
        self.assertIn("is NOT one of your principals", line)
