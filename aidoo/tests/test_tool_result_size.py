# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""A tool result must be bounded in BYTES, not only in rows.

``MAX_LIMIT`` caps how many records come back and says nothing about how large
one of them is, and the two are unrelated failure modes. A mail loop on
``cicdoo.mail`` produced individual ``mail.message`` bodies of 3.6 MB, and
Donna's inbox sweep reads message bodies, so a single row was enough to exceed
any sane context budget while sitting comfortably inside a 50-row limit.

The contract these tests pin: a result over the cap comes back reduced, still
shaped the way the caller expects, and *saying* it was reduced. Silent
truncation would be worse than the size itself, because the model would then
reason about a result it believes to be complete.
"""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.aidoo.models.aidoo_tool_exec import MAX_RESULT_BYTES


@tagged("post_install", "-at_install", "aidoo")
class TestToolResultSize(TransactionCase):

    def setUp(self):
        super().setUp()
        self.executor = self.env["aidoo.tool.exec"]

    def test_small_result_is_returned_untouched(self):
        result = {"records": [{"id": 1, "name": "fine"}], "returned": 1}
        self.assertIs(self.executor._cap_result(result), result,
                      "the cap must be free for the results that are the norm")

    def test_one_huge_field_is_clipped_and_the_row_survives(self):
        """The cicdoo.mail case: one row, one enormous body field.

        Dropping the row would lose the id, the sender and the date, which is
        most of what a sweep actually needs. Clipping the field keeps the row
        useful.
        """
        result = {"records": [{
            "id": 21533,
            "email_from": '"Greg Caleo" <notifications@cicdoo.com>',
            "body": "x" * (4 * 1024 * 1024),
        }], "returned": 1}

        capped = self.executor._cap_result(result)

        self.assertLessEqual(self.executor._result_bytes(capped),
                             MAX_RESULT_BYTES)
        self.assertEqual(len(capped["records"]), 1,
                         "the row is kept; only the oversized field is cut")
        row = capped["records"][0]
        self.assertEqual(row["id"], 21533)
        self.assertEqual(row["email_from"],
                         '"Greg Caleo" <notifications@cicdoo.com>',
                         "small fields must come through intact")
        self.assertIn("clipped", row["body"])
        self.assertIn("body", capped["truncated"],
                      "the model has to be told which field was cut")

    def test_many_big_rows_drop_rows_and_say_so(self):
        result = {"records": [{"id": i, "body": "y" * 20000}
                              for i in range(200)],
                  "returned": 200}

        capped = self.executor._cap_result(result)

        self.assertLessEqual(self.executor._result_bytes(capped),
                             MAX_RESULT_BYTES)
        self.assertLess(len(capped["records"]), 200)
        self.assertIn("dropped", capped["truncated"])
        self.assertTrue(capped["records"],
                        "something must come back; an empty result would read "
                        "as 'no such records' rather than 'too much data'")

    def test_oversized_result_with_no_rows_reports_an_error(self):
        """Nothing sensible to clip, so say what happened rather than guess."""
        capped = self.executor._cap_result({"blob": "z" * (2 * MAX_RESULT_BYTES)})
        self.assertTrue(capped["truncated"])
        self.assertIn("error", capped)

    def test_sql_rows_are_capped_too(self):
        """sql_select returns 'rows', not 'records', and has a 1000-row cap
        that bounds size no better than the 50-record one does."""
        result = {"rows": [{"id": i, "payload": "q" * 20000}
                           for i in range(200)],
                  "row_count": 200}

        capped = self.executor._cap_result(result)

        self.assertLessEqual(self.executor._result_bytes(capped),
                             MAX_RESULT_BYTES)
        self.assertIn("truncated", capped)
        self.assertLess(len(capped["rows"]), 200)
