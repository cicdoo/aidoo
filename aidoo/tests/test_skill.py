# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""AI skills — reusable instruction presets attached to a chat or scheduled agent.

Pins the model/ownership contract (owner default, manager-only `is_shared`,
own+shared visibility), the prompt-injection (an attached skill rides the per-turn
identity block for every provider), and the scheduled-agent wiring."""
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


def _no_launch(self, session, prompt, is_first, token):
    return None


@tagged("post_install", "-at_install", "aidoo")
class TestAidooSkill(TransactionCase):

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Skill User", "login": "aidoo_skill_user",
            "groups_id": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_user").id)],
        })
        self.other = self.env["res.users"].create({
            "name": "Other User", "login": "aidoo_skill_other",
            "groups_id": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_user").id)],
        })
        self.manager = self.env["res.users"].create({
            "name": "Skill Manager", "login": "aidoo_skill_manager",
            "groups_id": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_manager").id)],
        })

    # ------------------------------------------------------------------
    # Model / ownership
    # ------------------------------------------------------------------
    def test_owner_defaults_to_creator(self):
        skill = self.env["aidoo.skill"].with_user(self.user).create(
            {"name": "Reporter", "instructions": "Answer in a table."})
        self.assertEqual(skill.user_id, self.user)

    def test_is_shared_is_manager_only(self):
        Skill = self.env["aidoo.skill"]
        with self.assertRaises(AccessError):
            Skill.with_user(self.user).create({
                "name": "Pub", "instructions": "x", "is_shared": True})
        # A plain user also can't flip an existing one to shared.
        skill = Skill.with_user(self.user).create(
            {"name": "Priv", "instructions": "x"})
        with self.assertRaises(AccessError):
            skill.with_user(self.user).write({"is_shared": True})
        # A manager can publish.
        mskill = Skill.with_user(self.manager).create({
            "name": "Shared", "instructions": "x", "is_shared": True})
        self.assertTrue(mskill.is_shared)

    # ------------------------------------------------------------------
    # Record rules: own + shared visible; other users' private hidden
    # ------------------------------------------------------------------
    def test_visibility_own_and_shared_only(self):
        Skill = self.env["aidoo.skill"]
        mine = Skill.with_user(self.user).create(
            {"name": "Mine", "instructions": "x"})
        shared = Skill.with_user(self.manager).create(
            {"name": "Shared", "instructions": "x", "is_shared": True})
        others_private = Skill.with_user(self.other).create(
            {"name": "Theirs", "instructions": "x"})

        visible = Skill.with_user(self.user).search([]).ids
        self.assertIn(mine.id, visible)
        self.assertIn(shared.id, visible)
        self.assertNotIn(others_private.id, visible)
        # And it is not writable/readable as a record for this user.
        self.assertFalse(
            Skill.with_user(self.user).search([("id", "=", others_private.id)]))

    # ------------------------------------------------------------------
    # Injection: an attached skill rides the per-turn identity block
    # ------------------------------------------------------------------
    def test_identity_includes_attached_skill(self):
        skill = self.env["aidoo.skill"].create({
            "name": "Tabler", "instructions": "Always answer with a table."})
        block = self.user._ai_identity_prompt(set(), skills=skill)
        self.assertIn("AI SKILL: Tabler", block)
        self.assertIn("Always answer with a table.", block)
        # No skills -> no skill block.
        plain = self.user._ai_identity_prompt(set())
        self.assertNotIn("AI SKILL", plain)

    def test_identity_includes_every_attached_skill(self):
        skills = self.env["aidoo.skill"].create([
            {"name": "Tabler", "instructions": "Always answer with a table."},
            {"name": "French", "instructions": "Répondre en français."},
        ])
        block = self.user._ai_identity_prompt(set(), skills=skills)
        self.assertIn("AI SKILL: Tabler", block)
        self.assertIn("AI SKILL: French", block)
        self.assertIn("Répondre en français.", block)

    # ------------------------------------------------------------------
    # Scheduled agent applies its skill to the run session
    # ------------------------------------------------------------------
    def test_scheduled_run_applies_skills(self):
        with patch("odoo.addons.aidoo.models.providers.claude."
                   "AidooProviderClaude._is_authenticated", return_value=True), \
             patch("odoo.addons.aidoo.models.aidoo_runner."
                   "AiAssistantRunner._launch", _no_launch):
            skills = self.env["aidoo.skill"].with_user(self.user).create([
                {"name": "Daily", "instructions": "Summarize sales."},
                {"name": "Brief", "instructions": "Keep it short."},
            ])
            agent = self.env["aidoo.scheduled"].with_user(self.user).create({
                "name": "Nightly", "provider": "claude",
                "prompt": "go", "skill_ids": [(6, 0, skills.ids)],
            })
            session = agent.with_user(self.user)._run_once()
            self.assertEqual(session.skill_ids, skills)
            # The computed preview surfaces the instructions on the form.
            self.assertIn("Summarize sales.", agent.skill_preview)
            self.assertIn("Keep it short.", agent.skill_preview)

    # ------------------------------------------------------------------
    # Scheduled agent driven by a persona (provider/credentials override)
    # ------------------------------------------------------------------
    def test_scheduled_run_applies_persona(self):
        with patch("odoo.addons.aidoo.models.providers.claude."
                   "AidooProviderClaude._is_authenticated", return_value=True), \
             patch("odoo.addons.aidoo.models.aidoo_runner."
                   "AiAssistantRunner._launch", _no_launch):
            persona = self.env["aidoo.persona"].create({
                "name": "ReportBot", "provider": "claude",
                "instructions": "You are ReportBot.",
            })
            agent = self.env["aidoo.scheduled"].with_user(self.user).create({
                "name": "Nightly", "provider": "gemini",
                "prompt": "go", "persona_id": persona.id,
            })
            session = agent.with_user(self.user)._run_once()
            # The persona overrides the agent's own provider and supplies the
            # credential user; tools still run as the agent's Run As user.
            self.assertEqual(session.persona_id, persona)
            self.assertEqual(session.provider, "claude")
            self.assertEqual(session._credential_user(), persona.bot_user_id)
            self.assertEqual(session.user_id, self.user)

    def test_persona_onchange_mirrors_provider_and_skills(self):
        skills = self.env["aidoo.skill"].create([
            {"name": "One", "instructions": "a"},
            {"name": "Two", "instructions": "b"},
        ])
        persona = self.env["aidoo.persona"].create({
            "name": "MirrorBot", "provider": "claude",
            "skill_ids": [(6, 0, skills.ids)],
        })
        agent = self.env["aidoo.scheduled"].create({
            "name": "Nightly", "provider": "gemini", "prompt": "go"})
        agent.persona_id = persona
        agent._onchange_persona_id()
        self.assertEqual(agent.provider, "claude")
        self.assertEqual(agent.skill_ids, skills)
        # Clearing the persona leaves the mirrored values untouched.
        agent.persona_id = False
        agent._onchange_persona_id()
        self.assertEqual(agent.provider, "claude")
        self.assertEqual(agent.skill_ids, skills)
