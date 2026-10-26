# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""End-to-end execution of the shared tool executor (``aidoo.tool.exec``).

The provider adapters call ``_dispatch`` for every tool the model requests; it is
the single enforcement + audit choke point. These tests drive it as a real
non-super user and pin the contract: happy-path reads return data, every denial /
validation / unknown path returns a ``{"error": ...}`` dict (never raises), and an
audit row is written for each call. They complement ``test_tool_access`` (which
covers the *grant* computation) by exercising the *execution* path."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestToolExec(TransactionCase):

    def setUp(self):
        super().setUp()
        # A plain internal user (never superuser) — _dispatch hard-aborts under su.
        self.user = self.env["res.users"].create({
            "name": "Aidoo Exec Tester",
            "login": "aidoo_exec_tester",
            "group_ids": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_user").id)],
        })
        self.Tool = self.env["aidoo.tool"]
        self.session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude"})
        # The executor bound to the acting user (su=False).
        self.exec = self.env["aidoo.tool.exec"].with_user(self.user)
        # The audit log commits on a SEPARATE cursor (so it survives a rollback);
        # it can't see this uncommitted test session and would raise an FK error.
        # Audit behaviour is covered by test_tool_log; stub it out here so these
        # tests isolate the executor's return/enforcement contract.
        p = patch.object(type(self.env["aidoo.tool_log"]), "_record",
                         lambda self, vals: None)
        p.start()
        self.addCleanup(p.stop)

    def _grant(self, *names):
        tools = self.Tool.search([("name", "in", list(names))])
        existing = set(tools.mapped("name"))
        for n in names:
            if n not in existing:
                tools |= self.Tool.create({"name": n, "label": n})
        self.user.sudo().aidoo_tool_ids = [(6, 0, tools.ids)]

    # ------------------------------------------------------------------
    # Happy-path reads (default grant includes the read tools)
    # ------------------------------------------------------------------
    def test_orm_read_returns_records(self):
        res = self.exec._dispatch(
            self.session, "orm_read",
            {"model": "res.users", "ids": [self.user.id], "fields": ["login"]})
        self.assertNotIn("error", res)
        self.assertEqual(res["records"][0]["login"], "aidoo_exec_tester")

    def test_orm_search_read_returns_shape(self):
        res = self.exec._dispatch(
            self.session, "orm_search_read",
            {"model": "res.users", "domain": [["id", "=", self.user.id]],
             "fields": ["login"]})
        self.assertNotIn("error", res)
        self.assertEqual(res["returned"], 1)
        self.assertEqual(res["records"][0]["login"], "aidoo_exec_tester")
        self.assertIn("total", res)

    # ------------------------------------------------------------------
    # Denials / validation — every path returns {"error": ...}, never raises
    # ------------------------------------------------------------------
    def test_ungranted_write_tool_is_denied(self):
        # orm_write is not granted by default → AccessError, surfaced as error.
        res = self.exec._dispatch(
            self.session, "orm_write",
            {"model": "res.users", "ids": [self.user.id],
             "values": {"name": "x"}})
        self.assertIn("error", res)
        self.assertIn("AccessError", res["error"])

    def test_unknown_tool_is_rejected(self):
        res = self.exec._dispatch(self.session, "definitely_not_a_tool", {})
        self.assertIn("error", res)
        self.assertIn("Unknown tool", res["error"])

    def test_unknown_model_is_rejected(self):
        res = self.exec._dispatch(
            self.session, "orm_read",
            {"model": "no.such.model", "ids": [1]})
        self.assertIn("error", res)

    def test_sql_select_requires_analyst_group(self):
        # sql_select is read-only so it survives the default grant, but the
        # acting user is not in the AI SQL Analyst group → denied.
        self._grant("sql_select")
        res = self.exec._dispatch(
            self.session, "sql_select",
            {"query": "SELECT id FROM res_users LIMIT 1"})
        self.assertIn("error", res)
        self.assertIn("AccessError", res["error"])

    # ------------------------------------------------------------------
    # many2one static-domain enforcement on write tools
    # ------------------------------------------------------------------
    def _mailing_setup(self):
        """Grant what an AI user needs to create mailings, or skip the test
        when mass_mailing is not in the registry."""
        if "mailing.mailing" not in self.env:
            self.skipTest("mass_mailing not installed")
        group = self.env.ref("mass_mailing.group_mass_mailing_user",
                             raise_if_not_found=False)
        if group:
            self.user.sudo().group_ids = [(4, group.id)]
        self._grant("orm_create")

    def test_orm_create_rejects_m2o_domain_violation(self):
        # mailing_model_id carries the static domain
        # [('is_mailing_enabled', '=', True)]; the abstract 'base' model
        # violates it and used to produce a form-crashing record.
        self._mailing_setup()
        base_model = self.env["ir.model"]._get("base")
        res = self.exec._dispatch(
            self.session, "orm_create",
            {"model": "mailing.mailing",
             "values": {"subject": "guarded", "mailing_model_id": base_model.id}})
        self.assertIn("error", res)
        self.assertIn("does not satisfy the field domain", res["error"])

    def test_orm_create_allows_valid_m2o(self):
        self._mailing_setup()
        partner_model = self.env["ir.model"]._get("res.partner")
        res = self.exec._dispatch(
            self.session, "orm_create",
            {"model": "mailing.mailing",
             "values": {"subject": "guarded ok",
                        "email_from": "test@example.com",
                        "mailing_model_id": partner_model.id}})
        self.assertNotIn("error", res)
        self.assertTrue(res["ids"])

    def test_dispatch_aborts_under_superuser(self):
        # The su guard is a hard invariant: tools must never run as superuser.
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.env["aidoo.tool.exec"].sudo()._dispatch(
                self.session, "orm_read",
                {"model": "res.users", "ids": [self.user.id]})
