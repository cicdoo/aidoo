# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""The audit log is written on a SEPARATE committed cursor so it survives a
rollback of the failed tool call it describes. This test proves the row is
durably committed (visible from an independent cursor) rather than tied to the
caller's transaction."""
from odoo import api, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestToolLog(TransactionCase):

    def test_record_commits_on_separate_cursor(self):
        marker = "aidoo-test-%d" % id(self)
        self.env["aidoo.tool_log"]._record({
            "tool": "sql_select",
            "outcome": "access_denied",
            "error_message": marker,
        })
        # MVCC: our snapshot predates the separate-cursor commit, so look from a
        # fresh cursor. Finding the row proves it was committed independently.
        dbname = self.env.cr.dbname
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            logs = env["aidoo.tool_log"].search(
                [("error_message", "=", marker)])
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs.outcome, "access_denied")
            self.assertEqual(logs.tool, "sql_select")
            # Clean up so the committed row doesn't leak into other tests.
            logs.unlink()
            cr.commit()

    def test_record_coerces_non_json_arguments(self):
        marker = "aidoo-test-coerce-%d" % id(self)
        # A non-dict/list arguments value must be coerced, not raise.
        self.env["aidoo.tool_log"]._record({
            "tool": "orm_read",
            "outcome": "error",
            "error_message": marker,
            "arguments": "not-json-at-all",
        })
        dbname = self.env.cr.dbname
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            logs = env["aidoo.tool_log"].search(
                [("error_message", "=", marker)])
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs.arguments, {"_raw": "not-json-at-all"})
            logs.unlink()
            cr.commit()
