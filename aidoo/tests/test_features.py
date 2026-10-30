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


def _no_launch(self, session, prompt, is_first, token, pending_audio=None):
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

    def test_context_note_includes_selection(self):
        p1 = self.env["res.partner"].create({"name": "A"})
        p2 = self.env["res.partner"].create({"name": "B"})
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "context_model": "res.partner", "context_view_type": "kanban",
            "context_selected_ids": "[%s, %s]" % (p1.id, p2.id),
        })
        note = session._context_note()
        self.assertIn("kanban", note.lower())          # uses the actual view type
        self.assertIn("selected", note.lower())
        self.assertIn("orm_read", note)
        self.assertIn(str(p1.id), note)
        # The chip label reflects the selection count.
        self.assertIn("2 selected", session._context_label())

    def test_context_note_includes_groupby_and_filters(self):
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "context_model": "res.partner", "context_view_type": "list",
            "context_groupby": '["country_id", "is_company"]',
            "context_filters": "Companies · Country: Belgium",
        })
        note = session._context_note()
        self.assertIn("grouped by", note.lower())
        self.assertIn("country_id", note)
        self.assertIn("Belgium", note)            # the filter summary is surfaced

    def test_richer_context_only_first_turn(self):
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "context_model": "res.partner",
            "context_groupby": '["country_id"]',
        })
        self.assertTrue(session._context_note())
        session.turn_count = 1
        self.assertEqual(session._context_note(), "")

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

    # ------------------------------------------------------------------
    # Feature 1b — configurable prior-run context
    # ------------------------------------------------------------------
    def _msg(self, session, role, body):
        return self.env["aidoo.message"].create({
            "session_id": session.id, "role": role, "body": body})

    def _ctx_session(self, agent):
        """A session for the agent populated across three 'runs' separated by
        dividers (user/assistant pairs r1q/r1a … r3q/r3a)."""
        session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude",
            "scheduled_id": agent.id, "name": agent.name})
        self._msg(session, "user", "r1q")
        self._msg(session, "assistant", "r1a")
        self._msg(session, "divider", "run 2")
        self._msg(session, "user", "r2q")
        self._msg(session, "assistant", "r2a")
        self._msg(session, "divider", "run 3")
        self._msg(session, "user", "r3q")
        self._msg(session, "assistant", "r3a")
        return session

    def test_run_context_none_is_empty(self):
        agent = self._agent(context_mode="none")
        self.assertEqual(agent._run_context(self._ctx_session(agent)), "")

    def test_run_context_messages_window(self):
        agent = self._agent(context_mode="messages", context_amount=2)
        ctx = agent._run_context(self._ctx_session(agent))
        self.assertIn("CONTEXT FROM PREVIOUS SCHEDULED RUNS", ctx)
        self.assertIn("r3q", ctx)
        self.assertIn("r3a", ctx)
        self.assertNotIn("r2a", ctx)        # only the last 2 messages

    def test_run_context_runs_window(self):
        agent = self._agent(context_mode="runs", context_amount=1)
        ctx = agent._run_context(self._ctx_session(agent))
        self.assertIn("r3q", ctx)
        self.assertIn("r3a", ctx)
        self.assertNotIn("r2q", ctx)        # last run only
        # Two runs back includes run 2 but not run 1.
        agent.context_amount = 2
        ctx2 = agent._run_context(self._ctx_session(agent))
        self.assertIn("r2q", ctx2)
        self.assertNotIn("r1q", ctx2)

    def test_run_context_full(self):
        agent = self._agent(context_mode="full")
        ctx = agent._run_context(self._ctx_session(agent))
        for needle in ("r1q", "r2a", "r3q"):
            self.assertIn(needle, ctx)

    def test_context_prefix_reaches_prompt_not_stored(self):
        """A scheduled run with carried context feeds it into the model prompt
        but never into the stored user message body."""
        captured = {}

        def _capture(this, session, prompt, is_first, token, pending_audio=None):
            captured["prompt"] = prompt
            return None

        agent = self._agent(context_mode="messages", context_amount=2)
        first = agent._run_once()
        first.state = "done"
        with patch("odoo.addons.aidoo.models.aidoo_runner."
                   "AiAssistantRunner._launch", _capture):
            second = agent._run_once()
        # The prompt handed to the runner carries the prior-run context block…
        self.assertIn("CONTEXT FROM PREVIOUS SCHEDULED RUNS", captured["prompt"])
        # …but the stored user messages never contain it.
        bodies = second.message_ids.filtered(
            lambda m: m.role == "user").mapped("body")
        self.assertTrue(bodies)
        for b in bodies:
            self.assertNotIn("CONTEXT FROM PREVIOUS SCHEDULED RUNS", b or "")

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

    def test_email_delivery_uses_persona_identity(self):
        persona = self.env["aidoo.persona"].create({
            "name": "ReportBot", "provider": "claude",
            "email": "reportbot@example.com",
        })
        agent = self._agent(delivery="email", persona_id=persona.id)
        session = agent._run_once()
        self.env["aidoo.message"].create({
            "session_id": session.id, "role": "assistant", "body": "All good."})
        with patch("odoo.addons.mail.models.mail_mail.MailMail.send",
                   return_value=True):
            session._deliver_result(True)
        mail = self.env["mail.mail"].search([], order="id desc", limit=1)
        self.assertIn("reportbot@example.com", mail.email_from)
        self.assertIn("ReportBot", mail.email_from)
        self.assertEqual(mail.author_id, persona.partner_id)

    def test_chatter_delivery_posts_as_persona(self):
        persona = self.env["aidoo.persona"].create({
            "name": "ChatterBot", "provider": "claude",
            "email": "chatterbot@example.com",
        })
        partner = self.env["res.partner"].create({"name": "Acme Co"})
        # Chatter delivery posts as the run-as user, which needs write access
        # on the target record (standard mail posting rule).
        self.user.groups_id += self.env.ref("base.group_partner_manager")
        agent = self._agent(delivery="chatter", persona_id=persona.id,
                            res_model="res.partner", res_id=partner.id)
        session = agent._run_once()
        self.env["aidoo.message"].create({
            "session_id": session.id, "role": "assistant", "body": "Noted."})
        session._deliver_result(True)
        self.assertEqual(partner.message_ids[0].author_id, persona.partner_id)

    def test_chatter_delivery_without_persona_email_still_posts(self):
        """A persona with no email identity falls back to the default author
        (mail refuses authors without an email address)."""
        persona = self.env["aidoo.persona"].create({
            "name": "MuteBot", "provider": "claude",
        })
        partner = self.env["res.partner"].create({"name": "Beta Co"})
        self.user.groups_id += self.env.ref("base.group_partner_manager")
        agent = self._agent(delivery="chatter", persona_id=persona.id,
                            res_model="res.partner", res_id=partner.id)
        session = agent._run_once()
        self.env["aidoo.message"].create({
            "session_id": session.id, "role": "assistant", "body": "Noted."})
        before = len(partner.message_ids)
        session._deliver_result(True)
        self.assertEqual(len(partner.message_ids), before + 1)
        self.assertNotEqual(partner.message_ids[0].author_id,
                            persona.partner_id)

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

    def test_im_search_includes_personas(self):
        """Persona partners show up in Discuss's "New message" search even
        though their service users are portal (share) users."""
        persona = self.env["aidoo.persona"].create(
            {"name": "SearchBot", "provider": "claude"})
        Partner = self.env["res.partner"].with_user(self.user)
        ids = [p["id"] for p in
               Partner.im_search("SearchBot").get("res.partner", [])]
        self.assertIn(persona.partner_id.id, ids)
        # An empty search lists personas too.
        ids = [p["id"] for p in
               Partner.im_search("").get("res.partner", [])]
        self.assertIn(persona.partner_id.id, ids)
        # Users outside the AI group are not offered personas.
        outsider = self.env["res.users"].create({
            "name": "No AI", "login": "aidoo_no_ai",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        ids = [p["id"] for p in
               self.env["res.partner"].with_user(outsider)
               .im_search("SearchBot").get("res.partner", [])]
        self.assertNotIn(persona.partner_id.id, ids)

    def test_channel_invite_search_includes_personas(self):
        persona = self.env["aidoo.persona"].create(
            {"name": "InviteBot", "provider": "claude"})
        Partner = self.env["res.partner"].with_user(self.user)
        res = Partner.search_for_channel_invite("InviteBot")
        ids = [p["id"] for p in res["data"].get("res.partner", [])]
        self.assertIn(persona.partner_id.id, ids)
        # A persona already in the channel is not suggested again.
        channel = self.env["discuss.channel"].sudo().create(
            {"name": "Grp", "channel_type": "channel"})
        channel.sudo().add_members(partner_ids=[persona.partner_id.id])
        res = Partner.search_for_channel_invite(
            "InviteBot", channel_id=channel.id)
        ids = [p["id"] for p in res["data"].get("res.partner", [])]
        self.assertNotIn(persona.partner_id.id, ids)

    def test_dm_with_persona_answers_without_mention(self):
        """A 1:1 chat with a persona spawns a session on a plain message —
        no @mention required."""
        persona = self.env["aidoo.persona"].create(
            {"name": "DmBot", "provider": "claude"})
        channel = self.env["discuss.channel"].with_user(self.user).channel_get(
            [persona.partner_id.id])
        channel.with_user(self.user).message_post(
            body="hello there", message_type="comment",
            author_id=self.user.partner_id.id,
            subtype_xmlid="mail.mt_comment")
        session = self.env["aidoo.session"].search(
            [("discuss_channel_id", "=", channel.id)])
        self.assertEqual(len(session), 1)
        self.assertEqual(session.persona_id, persona)
        self.assertEqual(session.user_id, self.user)
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

    def _sessions_in(self, channel):
        return self.env["aidoo.session"].search(
            [("discuss_channel_id", "=", channel.id)])

    def test_non_comment_message_is_ignored(self):
        """Notifications/logs (non-comment) must not spawn a turn."""
        channel = self._channel_with_bot()
        channel.with_user(self.user).message_post(
            body="just a note", message_type="notification",
            author_id=self.user.partner_id.id)
        self.assertFalse(self._sessions_in(channel))

    def test_empty_body_is_ignored(self):
        channel = self._channel_with_bot()
        channel.with_user(self.user).message_post(
            body="   ", message_type="comment",
            author_id=self.user.partner_id.id, subtype_xmlid="mail.mt_comment")
        self.assertFalse(self._sessions_in(channel))

    def test_non_ai_user_is_ignored(self):
        """A human without the AI Assistant group gets no turn."""
        outsider = self.env["res.users"].create({
            "name": "No AI Grant", "login": "aidoo_no_grant",
            "groups_id": [(4, self.env.ref("base.group_user").id)],
        })
        bot = self.env["aidoo.session"]._aidoo_bot_partner()
        channel = self.env["discuss.channel"].with_user(
            outsider).channel_get([bot.id, outsider.partner_id.id])
        channel.with_user(outsider).message_post(
            body="help me", message_type="comment",
            author_id=outsider.partner_id.id, subtype_xmlid="mail.mt_comment")
        self.assertFalse(self._sessions_in(channel))

    def test_bot_absent_from_channel_is_ignored(self):
        """A channel the bot doesn't belong to is never handled."""
        channel = self.env["discuss.channel"].with_user(self.user).create({
            "name": "No Bot Here", "channel_type": "channel"})
        channel.with_user(self.user).message_post(
            body="anyone there?", message_type="comment",
            author_id=self.user.partner_id.id, subtype_xmlid="mail.mt_comment")
        self.assertFalse(self._sessions_in(channel))

    def test_unauthenticated_provider_prompts_to_connect(self):
        """When the author's provider isn't connected, the bot replies asking
        them to connect and does not start a run."""
        channel = self._channel_with_bot()
        before = len(channel.message_ids)
        with patch("odoo.addons.aidoo.models.providers.claude."
                   "AidooProviderClaude._is_authenticated", return_value=False):
            channel.with_user(self.user).message_post(
                body="hello", message_type="comment",
                author_id=self.user.partner_id.id,
                subtype_xmlid="mail.mt_comment")
        bot = self.env["aidoo.session"]._aidoo_bot_partner()
        new_msgs = channel.message_ids[:len(channel.message_ids) - before]
        self.assertTrue(any(m.author_id == bot and "connect" in
                            (m.body or "").lower() for m in new_msgs))
        # A session row may be created, but the run is never launched.
        self.assertFalse(self._sessions_in(channel).filtered(
            lambda s: s.state == "running"))


@tagged("post_install", "-at_install", "aidoo")
class TestAidooAudioUploads(TransactionCase):
    """Audio voice-note uploads: allow-list, transcript manifest, cached
    transcript replay and the never-crash failure path. Whisper itself is
    never spawned — the failure test points ``aidoo.whisper_python`` at a
    non-existent binary."""

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Aidoo Audio Tester",
            "login": "aidoo_audio_tester",
            "email": "audio.tester@example.com",
            "groups_id": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_user").id)],
        })
        self.session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude"})

    def test_audio_extensions_allowed_others_still_rejected(self):
        S = self.env["aidoo.session"]
        self.assertEqual(S._safe_upload_name("voice note.mp3"), "voice_note.mp3")
        self.assertEqual(S._safe_upload_name("memo.OGG"), "memo.OGG")
        with self.assertRaises(Exception):
            S._safe_upload_name("payload.exe")

    def test_compose_prompt_points_at_transcript(self):
        path = "/tmp/uploads/7_note.mp3"
        prompt = self.session._compose_prompt("what did I say?", [path])
        self.assertIn(path + ".transcript.txt", prompt)
        self.assertIn("Never try to Read the audio file itself", prompt)
        # Non-audio files keep the plain manifest line.
        self.assertNotIn(".transcript.txt",
                         self.session._compose_prompt("hi", ["/tmp/a.txt"]))

    def test_cached_transcript_replayed_on_materialize(self):
        from odoo.addons.aidoo.models.aidoo_session import TRANSCRIPT_MARKER
        desc = self.session.with_user(self.user)._store_upload(
            "note.mp3", b"\xff\xfbfake-mp3-bytes")
        att = self.env["ir.attachment"].browse(desc["id"])
        # No transcript yet -> the attachment is a pending job.
        paths, atts = self.session._materialize_attachments([att.id])
        self.assertEqual(
            [j["att_id"] for j in self.session._pending_audio_jobs(paths, atts)],
            [att.id])
        # Cache a transcript, re-materialize: file replayed, no pending job.
        att.sudo().description = TRANSCRIPT_MARKER + "hello from cache"
        paths, atts = self.session._materialize_attachments([att.id])
        tpath = self.session._transcript_path_for(paths[0])
        with open(tpath, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello from cache")
        self.assertFalse(self.session._pending_audio_jobs(paths, atts))

    def test_transcription_failure_writes_note_and_never_raises(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.whisper_python", "/nonexistent/python-binary")
        desc = self.session.with_user(self.user)._store_upload(
            "broken.mp3", b"\xff\xfbnot-really-audio")
        att = self.env["ir.attachment"].browse(desc["id"])
        paths, atts = self.session._materialize_attachments([att.id])
        jobs = self.session._pending_audio_jobs(paths, atts)
        self.session._transcribe_pending(jobs)  # must not raise
        tpath = self.session._transcript_path_for(paths[0])
        with open(tpath, encoding="utf-8") as f:
            note = f.read()
        self.assertIn("No transcript available", note)
        self.assertIn("broken.mp3", note)
        # Nothing cached -> retried on the next turn.
        self.assertFalse(att.sudo().description)


class TestAidooMoreUploads(TransactionCase):
    """Extra text/code extensions and spreadsheet -> text conversion."""

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Aidoo Upload Tester",
            "login": "aidoo_upload_tester",
            "email": "upload.tester@example.com",
            "groups_id": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_user").id)],
        })
        self.session = self.env["aidoo.session"].with_user(self.user).create({
            "user_id": self.user.id, "provider": "claude"})

    def test_new_text_extension_accepted(self):
        desc = self.session._store_upload("main.rs", b"fn main() {}")
        self.assertEqual(desc["name"], "main.rs")

    def test_executables_and_archives_rejected(self):
        S = self.env["aidoo.session"]
        for name in ("a.exe", "a.zip"):
            with self.assertRaises(Exception):
                S._safe_upload_name(name)

    def test_xlsx_converted_to_sheet_text(self):
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.title = "Data"
        wb.active.append(["name", "qty"])
        wb.active.append(["widget", 42])
        buf = io.BytesIO()
        wb.save(buf)
        desc = self.session._store_upload("book.xlsx", buf.getvalue())
        paths, _atts = self.session._materialize_attachments([desc["id"]])
        with open(self.session._sheet_text_path_for(paths[0]), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("--- sheet: Data ---", text)
        self.assertIn("widget\t42", text)
        self.assertIn(".sheet.txt", self.session._compose_prompt("x", paths))

    def test_corrupt_xlsx_leaves_note(self):
        desc = self.session._store_upload("bad.xlsx", b"not a workbook")
        paths, _atts = self.session._materialize_attachments([desc["id"]])
        with open(self.session._sheet_text_path_for(paths[0]), encoding="utf-8") as f:
            self.assertIn("Could not read this spreadsheet", f.read())
