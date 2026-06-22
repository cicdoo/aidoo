# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""The shared tool executor bounds each tool call's SQL with ``lock_timeout`` and
``statement_timeout`` on the request cursor, so a write blocked on a lock held
elsewhere self-aborts (and the request rolls back, releasing the lock) instead of
hanging the worker and leaving a zombie transaction after the loopback bridge has
already given up on its own 60s call timeout."""
import time

import psycopg2

from odoo import api, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestBridgeGuards(TransactionCase):

    def test_apply_stmt_guards_sets_cursor_timeouts(self):
        """_apply_stmt_guards reads the config and applies both timeouts (LOCAL)
        to the current cursor."""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("aidoo.tool_lock_timeout_ms", "1234")
        icp.set_param("aidoo.tool_statement_timeout_ms", "45678")

        self.env["aidoo.tool.exec"]._apply_stmt_guards()

        self.env.cr.execute("SHOW lock_timeout")
        self.assertEqual(self.env.cr.fetchone()[0], "1234ms")
        self.env.cr.execute("SHOW statement_timeout")
        self.assertEqual(self.env.cr.fetchone()[0], "45678ms")

    def test_zero_lock_timeout_is_skipped(self):
        """A 0 (disabled) config value leaves the cursor default untouched."""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("aidoo.tool_lock_timeout_ms", "0")
        icp.set_param("aidoo.tool_statement_timeout_ms", "0")
        self.env.cr.execute("SET LOCAL lock_timeout = 0")

        self.env["aidoo.tool.exec"]._apply_stmt_guards()

        self.env.cr.execute("SHOW lock_timeout")
        self.assertEqual(self.env.cr.fetchone()[0], "0")

    def test_blocked_write_self_aborts_under_lock_timeout(self):
        """End-to-end: with a bounded lock_timeout, a write contending for a row
        lock held by another connection raises quickly rather than hanging — the
        mechanism that prevents the zombie-lock that blocked module installs."""
        dbname = self.env.cr.dbname
        # A committed target row, on its own connection, visible to the others.
        setup = Registry(dbname).cursor()
        env = api.Environment(setup, SUPERUSER_ID, {})
        pid = env["res.partner"].create({"name": "Aidoo Lock Target"}).id
        setup.commit()
        setup.close()

        holder = Registry(dbname).cursor()
        writer = Registry(dbname).cursor()
        try:
            # Holder takes an exclusive row lock and keeps its transaction open.
            holder.execute("SELECT id FROM res_partner WHERE id = %s FOR UPDATE", (pid,))
            # Writer bounds its lock wait exactly as _apply_stmt_guards does.
            writer.execute("SET LOCAL lock_timeout = %s", (500,))
            t0 = time.time()
            with self.assertRaises(psycopg2.OperationalError):
                writer.execute(
                    "UPDATE res_partner SET comment = 'x' WHERE id = %s", (pid,))
            self.assertLess(time.time() - t0, 10,
                            "a blocked write must self-abort, not hang")
        finally:
            holder.rollback()
            holder.close()
            writer.rollback()
            writer.close()
            cleanup = Registry(dbname).cursor()
            cleanup.execute("DELETE FROM res_partner WHERE id = %s", (pid,))
            cleanup.commit()
            cleanup.close()
