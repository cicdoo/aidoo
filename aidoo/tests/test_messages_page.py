# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Newest-anchored message paging (`aidoo.session._messages_page`).

The chat client loads the most recent page first and fetches older messages on
scroll-up. These tests pin that contract: page size, chronological order within a
page, the `has_more` flag, the `before_id` cursor walking backwards without gaps
or overlaps, and the unlimited legacy path used by the floating companion."""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestMessagesPage(TransactionCase):

    def setUp(self):
        super().setUp()
        self.session = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "claude"})
        # 75 messages in known order; bodies encode their index for assertions.
        self.msgs = self.env["aidoo.message"].create([
            {"session_id": self.session.id, "role": "user", "body": "m%02d" % i}
            for i in range(75)
        ])

    def _bodies(self, page):
        return [m["body"] for m in page["messages"]]

    def test_first_page_is_newest_in_chronological_order(self):
        page = self.session._messages_page(limit=30)
        self.assertEqual(len(page["messages"]), 30)
        self.assertTrue(page["has_more"])
        # The newest 30 (m45..m74), returned oldest-first for display.
        self.assertEqual(self._bodies(page)[0], "m45")
        self.assertEqual(self._bodies(page)[-1], "m74")

    def test_before_id_walks_back_without_gaps(self):
        first = self.session._messages_page(limit=30)
        oldest_loaded = first["messages"][0]["id"]
        page2 = self.session._messages_page(limit=30, before_id=oldest_loaded)
        self.assertEqual(len(page2["messages"]), 30)
        self.assertTrue(page2["has_more"])
        # Contiguous with the first page: m15..m44, no overlap with m45.
        self.assertEqual(self._bodies(page2)[0], "m15")
        self.assertEqual(self._bodies(page2)[-1], "m44")
        self.assertLess(page2["messages"][-1]["id"], oldest_loaded)

    def test_last_page_clears_has_more(self):
        # 75 messages, page 30 -> remaining 15 on the third page.
        p1 = self.session._messages_page(limit=30)
        p2 = self.session._messages_page(
            limit=30, before_id=p1["messages"][0]["id"])
        p3 = self.session._messages_page(
            limit=30, before_id=p2["messages"][0]["id"])
        self.assertEqual(len(p3["messages"]), 15)
        self.assertFalse(p3["has_more"])
        self.assertEqual(self._bodies(p3)[0], "m00")

    def test_no_limit_returns_everything(self):
        page = self.session._messages_page()
        self.assertEqual(len(page["messages"]), 75)
        self.assertFalse(page["has_more"])

    def test_limit_is_capped(self):
        from odoo.addons.aidoo.models.aidoo_session import AIDOO_MSG_PAGE_MAX
        page = self.session._messages_page(limit=10_000)
        # Capped, but here total < cap so all 75 come back with no more history.
        self.assertLessEqual(len(page["messages"]), AIDOO_MSG_PAGE_MAX)
        self.assertEqual(len(page["messages"]), 75)
        self.assertFalse(page["has_more"])
