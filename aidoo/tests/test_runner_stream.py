# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""The generic runner applies a stream of normalized ops to the DB/bus. These
tests pin the message bookkeeping — in particular that a final ``done`` event
which repeats the full assistant text does NOT create a second message when the
text already streamed (the 'reply shown twice' regression)."""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestRunnerStream(TransactionCase):

    def setUp(self):
        super().setUp()
        self.runner = self.env["aidoo.runner"]
        self.session = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "claude"})

    def _apply(self, ops):
        msg_map, produced = {}, {"any": False}
        for op in ops:
            self.runner._apply_op(self.env, self.session, op, msg_map, produced)
        return self.session.message_ids.filtered(lambda m: m.role == "assistant")

    def test_done_text_does_not_duplicate_streamed_message(self):
        """Streamed text under a CLI msg id + a final done repeating that text
        (msg_id=None) must yield exactly ONE assistant message."""
        msgs = self._apply([
            {"op": "text", "msg_id": "m1", "text": "Hello there."},
            {"op": "done", "msg_id": None, "text": "Hello there."},
        ])
        self.assertEqual(len(msgs), 1, "the reply must not be shown twice")
        self.assertEqual(msgs.body, "Hello there.")

    def test_done_text_is_used_as_fallback_when_nothing_streamed(self):
        """If no text streamed (e.g. a CLI that only returns a final result),
        the done text becomes the single assistant message."""
        msgs = self._apply([
            {"op": "done", "msg_id": None, "text": "Final answer."},
        ])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs.body, "Final answer.")

    def test_streamed_text_blocks_append_into_one_message(self):
        """Multiple text blocks for the same CLI message id append, not split."""
        msgs = self._apply([
            {"op": "text", "msg_id": "m1", "text": "Hello "},
            {"op": "text", "msg_id": "m1", "text": "world"},
            {"op": "done", "msg_id": None, "text": "Hello world"},
        ])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs.body, "Hello world")
