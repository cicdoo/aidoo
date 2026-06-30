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

    # --- denylist enforcement on the SQL tool ------------------------------
    def _exec(self):
        return self.env["aidoo.tool.exec"]

    def test_denylist_blocks_matching_table(self):
        from odoo.exceptions import AccessError
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.sql_table_denylist", "account_*, hr_*")
        with self.assertRaises(AccessError):
            self._exec()._check_sql_denylist("SELECT id FROM account_move")
        # A quoted name can't slip past it.
        with self.assertRaises(AccessError):
            self._exec()._check_sql_denylist('SELECT id FROM "account_move_line"')

    def test_denylist_allows_other_tables(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.sql_table_denylist", "account_*, hr_*")
        # No exception for a non-denied table, even if a column starts with the
        # denied prefix.
        self._exec()._check_sql_denylist("SELECT account_id FROM sale_order")

    def test_denylist_empty_is_noop(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.sql_table_denylist", "")
        self._exec()._check_sql_denylist("SELECT id FROM account_move")
