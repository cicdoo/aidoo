# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import api, fields, models, _
from odoo.exceptions import AccessError


class AidooSkill(models.Model):
    """A reusable instruction preset ("skill") a user can attach to a chat
    conversation or a scheduled agent.

    A skill is just a name + an instruction body. When a session/agent has a
    skill, its instructions are injected into the model prompt every turn (via
    the shared identity block in ``res.users._ai_identity_prompt``), so every
    provider follows it identically. Skills are personal by default; a manager
    may flag one ``is_shared`` to publish it to all users.
    """
    _name = "aidoo.skill"
    _description = "Aidoo AI Skill (instruction preset)"
    _order = "name"

    name = fields.Char(required=True, index=True, help="Skill name, e.g. 'Sales Analyst'.")
    description = fields.Char(
        help="Short one-line summary shown in the skill picker.")
    instructions = fields.Text(
        required=True,
        help="The instruction text injected into the assistant's prompt every "
             "turn while this skill is attached.")
    user_id = fields.Many2one(
        "res.users", string="Owner", required=True, ondelete="cascade",
        default=lambda self: self.env.user,
        help="User who authored this skill.")
    is_shared = fields.Boolean(
        string="Shared",
        help="Publish this skill to all AI Assistant users (managers only). "
             "Shared skills are read-only to everyone but their owner/managers.")
    active = fields.Boolean(default=True)

    # ------------------------------------------------------------------
    # is_shared is a manager-only flag (publishing to all users)
    # ------------------------------------------------------------------
    def _check_share_rights(self, vals):
        if vals.get("is_shared") and not (
                self.env.su or self.env.user.has_group("aidoo.group_ai_manager")):
            raise AccessError(_(
                "Only an AI Assistant Manager can publish a skill to all users."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_share_rights(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._check_share_rights(vals)
        return super().write(vals)
