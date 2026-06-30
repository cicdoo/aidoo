# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""The read-only SQL validator is the single most security-sensitive surface in
Aidoo: it is the text gate that, together with Postgres' ``SET TRANSACTION
READ ONLY``, keeps ``sql_select`` from ever mutating data. These tests pin its
behaviour so a future refactor can't silently widen it."""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.aidoo.controllers.tools import (
    _validate_select,
    _strip_sql_literals,
    _sql_table_refs,
)


@tagged("post_install", "-at_install", "aidoo")
class TestSqlGuard(TransactionCase):

    # --- accepted -----------------------------------------------------------
    def test_plain_select_allowed(self):
        self.assertTrue(_validate_select("SELECT id, name FROM res_partner"))

    def test_with_cte_allowed(self):
        self.assertTrue(_validate_select(
            "WITH t AS (SELECT id FROM res_users) SELECT * FROM t"))

    def test_trailing_semicolon_allowed(self):
        # A single trailing semicolon is stripped, not treated as stacking.
        self.assertTrue(_validate_select("SELECT 1;"))

    def test_keyword_inside_string_literal_allowed(self):
        # 'delete' appears only inside a string literal → must not trip the
        # write-keyword guard once literals are stripped.
        self.assertTrue(_validate_select(
            "SELECT id FROM res_partner WHERE name = 'please delete me'"))

    # --- rejected: not a SELECT --------------------------------------------
    def test_non_select_rejected(self):
        for q in ("UPDATE res_users SET active = false",
                  "DELETE FROM res_partner",
                  "INSERT INTO res_partner (name) VALUES ('x')",
                  "DROP TABLE res_users",
                  "TRUNCATE res_partner"):
            with self.assertRaises(ValidationError, msg=q):
                _validate_select(q)

    # --- rejected: stacked statements --------------------------------------
    def test_stacked_statements_rejected(self):
        with self.assertRaises(ValidationError):
            _validate_select("SELECT 1; DROP TABLE res_users")

    # --- rejected: write keywords hidden after a SELECT --------------------
    def test_cte_writes_rejected(self):
        # A data-modifying CTE is still a write; the keyword guard must catch it.
        with self.assertRaises(ValidationError):
            _validate_select(
                "WITH x AS (DELETE FROM res_partner RETURNING id) SELECT * FROM x")

    # --- rejected: row locks -----------------------------------------------
    def test_for_update_rejected(self):
        with self.assertRaises(ValidationError):
            _validate_select("SELECT id FROM res_partner FOR UPDATE")

    def test_for_share_rejected(self):
        with self.assertRaises(ValidationError):
            _validate_select("SELECT id FROM res_partner FOR SHARE")

    # --- rejected: dangerous functions -------------------------------------
    def test_dangerous_functions_rejected(self):
        for q in ("SELECT pg_read_file('/etc/passwd')",
                  "SELECT * FROM dblink('x', 'y')",
                  "SELECT lo_import('/etc/passwd')",
                  "SELECT pg_sleep(10)",
                  "SELECT setval('seq', 1)"):
            with self.assertRaises(ValidationError, msg=q):
                _validate_select(q)

    # --- rejected: size / emptiness ----------------------------------------
    def test_empty_rejected(self):
        with self.assertRaises(ValidationError):
            _validate_select("")

    def test_oversized_rejected(self):
        with self.assertRaises(ValidationError):
            _validate_select("SELECT 1 WHERE " + "a" * 20001)

    # --- literal stripping helper ------------------------------------------
    def test_strip_literals_removes_comments_and_strings(self):
        stripped = _strip_sql_literals(
            "SELECT 1 -- drop table\n, '/* delete */' FROM t /* update */")
        self.assertNotIn("drop", stripped.lower())
        self.assertNotIn("delete", stripped.lower())
        self.assertNotIn("update", stripped.lower())

    # --- table-reference extraction (powers the configurable denylist) ------
    def test_table_refs_simple(self):
        self.assertEqual(
            _sql_table_refs("SELECT id FROM account_move"), {"account_move"})

    def test_table_refs_join_and_comma(self):
        self.assertEqual(
            _sql_table_refs("SELECT * FROM sale_order s "
                            "JOIN account_move m ON m.id = s.id"),
            {"sale_order", "account_move"})
        self.assertEqual(
            _sql_table_refs("SELECT * FROM account_move m, res_partner p"),
            {"account_move", "res_partner"})

    def test_table_refs_schema_and_quoted(self):
        self.assertEqual(
            _sql_table_refs("SELECT * FROM public.account_move"), {"account_move"})
        self.assertEqual(
            _sql_table_refs('SELECT * FROM "account_move"'), {"account_move"})

    def test_table_refs_ignores_columns(self):
        # account_id is a column, not a table — must NOT be reported.
        refs = _sql_table_refs("SELECT account_id, amount FROM sale_order")
        self.assertEqual(refs, {"sale_order"})

    def test_table_refs_subquery_and_cte(self):
        self.assertIn(
            "account_move",
            _sql_table_refs("SELECT * FROM (SELECT id FROM account_move) x"))
        self.assertIn(
            "account_move",
            _sql_table_refs("WITH t AS (SELECT id FROM account_move) "
                            "SELECT * FROM t"))

    # --- per-user SQL access (analyst denylist + non-analyst allowlist) -----
    def _exec_as(self, user):
        return self.env["aidoo.tool.exec"].with_user(user)

    def _mk_user(self, login, analyst=False, allowlist=None):
        groups = [self.env.ref("base.group_user").id,
                  self.env.ref("aidoo.group_ai_user").id]
        if analyst:
            groups.append(self.env.ref("aidoo.group_ai_sql_analyst").id)
        u = self.env["res.users"].create({
            "name": login, "login": login,
            "group_ids": [(6, 0, groups)],
        })
        if allowlist is not None:
            u.aidoo_sql_table_allowlist = allowlist
        return u

    def test_analyst_blocked_by_global_denylist(self):
        from odoo.exceptions import AccessError
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.sql_table_denylist", "account_*, hr_*")
        analyst = self._mk_user("aidoo_sql_analyst1", analyst=True)
        ex = self._exec_as(analyst)
        with self.assertRaises(AccessError):
            ex._check_sql_access("SELECT id FROM account_move")
        with self.assertRaises(AccessError):       # quoted can't slip past
            ex._check_sql_access('SELECT id FROM "account_move_line"')
        # Non-denied table is fine, even with a column sharing the prefix.
        ex._check_sql_access("SELECT account_id FROM sale_order")

    def test_non_analyst_without_allowlist_is_denied(self):
        from odoo.exceptions import AccessError
        user = self._mk_user("aidoo_sql_none")
        with self.assertRaises(AccessError):
            self._exec_as(user)._check_sql_access("SELECT id FROM sale_order")

    def test_non_analyst_allowlist_scopes_access(self):
        from odoo.exceptions import AccessError
        user = self._mk_user("aidoo_sql_acct", allowlist="account_*")
        ex = self._exec_as(user)
        # Allowed: a table matching the per-user allowlist.
        ex._check_sql_access("SELECT id FROM account_move")
        ex._check_sql_access('SELECT id FROM "account_move_line" l')
        # Denied: a table outside the allowlist.
        with self.assertRaises(AccessError):
            ex._check_sql_access("SELECT id FROM sale_order")

    def test_sql_disabled_blocks_everyone(self):
        from odoo.exceptions import AccessError
        self.env["ir.config_parameter"].sudo().set_param("aidoo.sql_enabled", "False")
        try:
            analyst = self._mk_user("aidoo_sql_analyst2", analyst=True)
            with self.assertRaises(AccessError):
                self._exec_as(analyst)._check_sql_access("SELECT id FROM res_users")
        finally:
            self.env["ir.config_parameter"].sudo().set_param("aidoo.sql_enabled", "True")
