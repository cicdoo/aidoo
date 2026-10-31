# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Model-written conversation titles (runner tag extraction)."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestTitleTag(TransactionCase):

    def setUp(self):
        super().setUp()
        self.runner = self.env["aidoo.runner"]
        self.session = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "claude"})

    def _run(self, ops, is_first=True):
        tstate = self.runner._title_state({"is_first": is_first})
        msg_map, produced = {}, {"any": False}
        for raw in ops:
            for op in self.runner._title_filter(self.session, tstate, raw):
                self.runner._apply_op(self.env, self.session, op, msg_map, produced)
        for op in self.runner._title_release(tstate, done=True):
            self.runner._apply_op(self.env, self.session, op, msg_map, produced)
        return "".join(self.session.message_ids.filtered(
            lambda m: m.role == "assistant").mapped("body"))

    def _text(self, *chunks):
        return [{"op": "text", "msg_id": "m1", "text": c} for c in chunks]

    def test_tag_in_one_chunk(self):
        body = self._run(self._text("<aidoo_title>Sales report</aidoo_title>\nHere it is."))
        self.assertEqual(body, "Here it is.")
        self.assertEqual(self.session.name, "Sales report")

    def test_tag_split_across_chunks(self):
        body = self._run(self._text(
            "<aid", "oo_title>Monthly ", "invoices</aidoo", "_title>\n\nHello", " there"))
        self.assertEqual(body, "Hello there")
        self.assertEqual(self.session.name, "Monthly invoices")

    def test_no_tag_keeps_default_name_and_text(self):
        default = self.session.name
        body = self._run(self._text("Hello", " world"))
        self.assertEqual(body, "Hello world")
        self.assertEqual(self.session.name, default)

    def test_partial_tag_prefix_is_released(self):
        body = self._run(self._text("<aid"))
        self.assertEqual(body, "<aid")

    def test_unclosed_tag_is_released_after_limit(self):
        long = "<aidoo_title>" + "x" * 400
        self.assertEqual(self._run(self._text(long)), long)

    def test_existing_name_is_kept_but_tag_stripped(self):
        self.session.name = "My chat"
        body = self._run(self._text("<aidoo_title>Other</aidoo_title>Answer"))
        self.assertEqual(body, "Answer")
        self.assertEqual(self.session.name, "My chat")

    def test_not_first_turn_is_untouched(self):
        text = "<aidoo_title>Other</aidoo_title>Answer"
        self.assertEqual(self._run(self._text(text), is_first=False), text)

    def test_replace_and_done_text_are_stripped(self):
        body = self._run([
            {"op": "text", "msg_id": "m1", "text": "<aidoo_title>T1</aidoo_title>A", "replace": True}])
        self.assertEqual(body, "A")
        self.assertEqual(self.session.name, "T1")
        s2 = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "claude"})
        self.session = s2
        body = self._run([{"op": "done", "text": "<aidoo_title>T2</aidoo_title>B"}])
        self.assertEqual(body, "B")
        self.assertEqual(s2.name, "T2")

    def test_send_does_not_rename_from_user_text(self):
        captured = {}

        def _capture(self_, session, prompt, is_first, token, pending_audio=None):
            captured["prompt"] = prompt

        with patch("odoo.addons.aidoo.models.aidoo_runner.AiAssistantRunner._launch",
                   _capture), \
                patch("odoo.addons.aidoo.models.providers.claude."
                      "AidooProviderClaude._is_authenticated", return_value=True):
            self.session.send_message("What are my open invoices?")
        self.assertEqual(self.session.name, "New conversation")
        self.assertIn("<aidoo_title>", captured["prompt"])
        self.assertNotIn("<aidoo_title>", self.session.message_ids[0].body)
