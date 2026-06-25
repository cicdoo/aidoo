# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Feature coverage for scheduled agents, the record-context note and the Discuss
bot. The CLI is never spawned — ``aidoo.runner._launch`` and the provider auth
probe are patched so we exercise the wiring, not a real provider."""
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


def _no_launch(self, session, prompt, is_first, token):
    """Stand-in for the runner: skip spawning a CLI (the session is already
    flipped to 'running' by send_message before this is called)."""
    return None


@tagged("post_install", "-at_install", "aidoo")
class TestAidooFeatures(TransactionCase):

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Aidoo Feature Tester",
            "login": "aidoo_feature_tester",
            "email": "feature.tester@example.com",
            "groups_id": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_user").id)],
        })
        # Pretend the (default) Claude provider is connected for this user.
        self.auth_patch = patch(
            "odoo.addons.aidoo.models.providers.claude.AidooProviderClaude."
            "_is_authenticated", return_value=True)
        self.launch_patch = patch(
            "odoo.addons.aidoo.models.aidoo_runner.AiAssistantRunner._launch",
            _no_launch)
        self.auth_patch.start()
        self.launch_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.addCleanup(self.launch_patch.stop)

    # ------------------------------------------------------------------
    # Feature 2 — record-context note
    # ------------------------------------------------------------------
    def test_context_note_only_first_turn_and_readable(self):
        partner = self.env["res.partner"].create({"name": "Acme Co"})
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "context_model": "res.partner", "context_res_id": partner.id,
        })
        note = session._context_note()
        self.assertIn("res.partner", note)
        self.assertIn("Acme Co", note)
        self.assertIn(note, session._compose_prompt("hello", []))
        # After the first turn no further context note is injected.
        session.turn_count = 1
        self.assertEqual(session._context_note(), "")

    def test_model_level_context_for_list(self):
        # No res_id (a list view): label is the model name, note is model-scoped.
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "context_model": "res.partner",
        })
        self.assertTrue(session._context_label())  # the model's display name
        note = session._context_note()
        self.assertIn("res.partner", note)
        self.assertIn("list", note.lower())

    def test_list_context_includes_domain(self):
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "context_model": "res.partner",
            "context_domain": '[["is_company", "=", true]]',
        })
        note = session._context_note()
        self.assertIn("is_company", note)
        self.assertIn("orm_search_read", note)

    def test_context_label_empty_for_missing_record(self):
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "context_model": "res.partner", "context_res_id": 999999999,
        })
        # display_name on a non-existent id is falsy → no leak, empty label.
        self.assertEqual(session._context_label(), "")

    # ------------------------------------------------------------------
    # Feature 1 — scheduled agents
    # ------------------------------------------------------------------
    def _agent(self, **kw):
        vals = {
            "name": "Daily summary", "user_id": self.user.id,
            "provider": "claude", "prompt": "summarize",
            "interval_number": 1, "interval_type": "days",
        }
        vals.update(kw)
        return self.env["aidoo.scheduled"].create(vals)

    def test_run_once_creates_session_and_launches(self):
        agent = self._agent()
        session = agent._run_once()
        self.assertEqual(session.scheduled_id, agent)
        self.assertEqual(session.user_id, self.user)
        # send_message flips the session to "running" right before launching.
        self.assertEqual(session.state, "running")
        self.assertTrue(agent.last_run)
        self.assertGreater(agent.next_run, fields.Datetime.now())

    def test_cron_runs_due_and_reschedules(self):
        past = fields.Datetime.subtract(fields.Datetime.now(), days=1)
        agent = self._agent(next_run=past)
        self.env["aidoo.scheduled"]._cron_run_due()
        self.assertEqual(len(agent.session_ids), 1)
        self.assertGreater(agent.next_run, fields.Datetime.now())

    def test_cron_skips_not_due(self):
        future = fields.Datetime.add(fields.Datetime.now(), days=1)
        agent = self._agent(next_run=future)
        self.env["aidoo.scheduled"]._cron_run_due()
        self.assertFalse(agent.session_ids)

    def test_runs_reuse_one_session_isolated(self):
        """Every run lands in the same conversation, but each is isolated: a
        fresh CLI id, a reset turn counter, and a divider between runs."""
        agent = self._agent()
        first = agent._run_once()
        first_cli = first.cli_session_id
        # Simulate the first turn finishing so the next run isn't deferred.
        first.state = "done"
        second = agent._run_once()
        self.assertEqual(second, first)                      # reused, not new
        self.assertEqual(len(agent.session_ids), 1)
        self.assertNotEqual(second.cli_session_id, first_cli)  # fresh CLI run
        self.assertEqual(second.turn_count, 0)               # treated as first
        self.assertEqual(
            second.message_ids.filtered(lambda m: m.role == "divider").mapped(
                "role"), ["divider"])

    def test_run_defers_while_previous_still_running(self):
        """A new tick must not stack onto a session that is still running."""
        agent = self._agent()
        session = agent._run_once()       # leaves the session 'running'
        self.assertEqual(session.state, "running")
        again = agent._run_once()
        self.assertEqual(again, session)
        self.assertEqual(len(agent.session_ids), 1)
        # No run boundary and no second user turn were added while deferred.
        self.assertFalse(session.message_ids.filtered(
            lambda m: m.role == "divider"))

    def test_email_delivery_creates_mail(self):
        agent = self._agent(delivery="email")
        session = agent._run_once()
        self.env["aidoo.message"].create({
            "session_id": session.id, "role": "assistant", "body": "All good."})
        before = self.env["mail.mail"].search_count([])
        # Stub send so the auto_delete=True mail isn't removed before we count it.
        with patch("odoo.addons.mail.models.mail_mail.MailMail.send",
                   return_value=True):
            session._deliver_result(True)
        self.assertEqual(self.env["mail.mail"].search_count([]), before + 1)

    def test_chat_delivery_sends_no_mail(self):
        agent = self._agent(delivery="chat")
        session = agent._run_once()
        self.env["aidoo.message"].create({
            "session_id": session.id, "role": "assistant", "body": "hi"})
        before = self.env["mail.mail"].search_count([])
        session._deliver_result(True)
        self.assertEqual(self.env["mail.mail"].search_count([]), before)

    # ------------------------------------------------------------------
    # Feature 3 — Discuss bot
    # ------------------------------------------------------------------
    def _channel_with_bot(self):
        bot = self.env["aidoo.session"]._aidoo_bot_partner()
        return self.env["discuss.channel"].with_user(self.user).channel_get(
            [bot.id, self.user.partner_id.id])

    def test_user_message_spawns_session(self):
        channel = self._channel_with_bot()
        channel.with_user(self.user).message_post(
            body="list my leads", message_type="comment",
            author_id=self.user.partner_id.id, subtype_xmlid="mail.mt_comment")
        session = self.env["aidoo.session"].search([
            ("discuss_channel_id", "=", channel.id),
            ("user_id", "=", self.user.id)])
        self.assertEqual(len(session), 1)
        self.assertEqual(session.state, "running")

    def test_bot_own_message_does_not_recurse(self):
        channel = self._channel_with_bot()
        bot = self.env["aidoo.session"]._aidoo_bot_partner()
        channel.sudo().message_post(
            body="hello from me", message_type="comment",
            author_id=bot.id, subtype_xmlid="mail.mt_comment")
        self.assertFalse(self.env["aidoo.session"].search([
            ("discuss_channel_id", "=", channel.id)]))

    def test_reply_posted_back_to_channel(self):
        channel = self._channel_with_bot()
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "discuss_channel_id": channel.id,
        })
        self.env["aidoo.message"].create({
            "session_id": session.id, "role": "assistant", "body": "Here you go."})
        before = len(channel.message_ids)
        session._deliver_result(True)
        bot = self.env["aidoo.session"]._aidoo_bot_partner()
        self.assertEqual(len(channel.message_ids), before + 1)
        self.assertEqual(channel.message_ids[0].author_id, bot)
